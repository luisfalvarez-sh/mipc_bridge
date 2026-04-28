import os
import time
import subprocess
import sys
import socket
import logging
import threading
import signal
from logging.handlers import RotatingFileHandler
from mipc_camera_client import MipcCameraClient

# ==========================================
#      SISTEMA DE LOGS (v31.10 - STABLE)
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("MIPC_BRIDGE")
# Añadir fichero de logs rotativo en /app/logs/bridge.log
try:
    os.makedirs('/app/logs', exist_ok=True)
    fh = RotatingFileHandler('/app/logs/bridge.log', maxBytes=5 * 1024 * 1024, backupCount=3)
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
    logger.addHandler(fh)
except Exception:
    # Si no se puede crear el handler, seguir con la salida a stdout
    logger.debug('No se pudo crear RotatingFileHandler en /app/logs')

# Guardamos el descriptor del FIFO para poder cerrarlo en el shutdown
FIFO_KEEPER = None

# ==========================================
#      CONFIGURACIÓN (adaptada a .env y rutas nuevas)
# ==========================================
CONFIG_DIR = "/app/config"
CONFIG_ENV = "/app/.env"
FIFO_PATH = "/tmp/mipc_fifo"
PLACEHOLDER_PATH = "/app/assets/reconnecting.mp4"

# Cargar `.env` del contenedor si existe.
try:
    from dotenv import load_dotenv
    if os.path.exists(CONFIG_ENV):
        load_dotenv(CONFIG_ENV)
except Exception:
    pass

def load_setting(key, default=None, mandatory=False):
    val = os.getenv(key, default)
    if mandatory and (val is None or val == ""):
        logger.error(f"Missing mandatory config '{key}'")
        sys.exit(1)
    return val

CAM_IP   = load_setting('CAM_IP', mandatory=True)
CAM_USER = load_setting('CAM_USER', mandatory=True)
CAM_PASS = load_setting('CAM_PASS', mandatory=True)
CAM_PORT = int(load_setting('CAM_PORT', default=7010))

RTSP_HOST = "mediamtx"
RTSP_LOCAL = f"rtsp://{RTSP_HOST}:8554/1"

PROCESOS = {"maestro": None, "fuente": None}

def check_port(ip, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.2)
        return s.connect_ex((ip, port)) == 0

def aniquilar(llave):
    proc = PROCESOS.get(llave)
    if proc:
        try:
            proc.terminate()
            proc.wait(timeout=1)
        except:
            try: proc.kill()
            except: pass
    PROCESOS[llave] = None


def _shutdown(signum, frame):
    logger.info(f"Recibido señal {signum}, deteniendo procesos...")
    try:
        aniquilar('fuente')
        aniquilar('maestro')
    except Exception as e:
        logger.error(f"Error al aniquilar procesos: {e}")
    global FIFO_KEEPER
    try:
        if FIFO_KEEPER:
            try:
                FIFO_KEEPER.close()
            except Exception:
                pass
            try:
                if os.path.exists(FIFO_PATH):
                    os.remove(FIFO_PATH)
            except Exception:
                pass
    except Exception as e:
        logger.error(f"Error during FIFO cleanup: {e}")
    sys.exit(0)

def iniciar_maestro():
    """
    MAESTRO PURO (Tablet Nueva / Web):
    Copiado directo absoluto. Sin filtros para evitar errores de FFmpeg.
    """
    aniquilar("maestro")
    logger.info("[*] Iniciando Maestro 1080p (Copia Directa)...")
    cmd = [
        'ffmpeg', '-y', '-nostdin', '-loglevel', 'error',
        '-fflags', '+genpts+igndts+flush_packets',
        '-f', 'mpegts', '-i', FIFO_PATH,
        '-c:v', 'copy',
        '-bsf:v', 'h264_mp4toannexb,dump_extra',
        '-c:a', 'copy',
        '-f', 'rtsp', '-rtsp_transport', 'tcp', RTSP_LOCAL
    ]
    PROCESOS["maestro"] = subprocess.Popen(cmd)

def loop_servidor_mjpeg():
    """
    SERVIDOR MJPEG (Tablet Vieja):
    Lee del stream RTSP local generado por el Maestro.
    Si la tablet falla, este proceso se reinicia sin afectar al Maestro.
    """
    logger.info("[MJPEG] Servidor 8080 listo. Esperando conexión de tablet vieja...")
    while True:
        # Esperamos a que el RTSP local tenga señal antes de intentar codificar MJPEG
        if check_port(RTSP_HOST, 8554):
            cmd = [
                'ffmpeg', '-y', '-nostdin', '-loglevel', 'quiet',
                '-rtsp_transport', 'tcp',
                '-c:v', 'h264_v4l2m2m', '-i', RTSP_LOCAL,
                '-vf', 'scale=640:360,fps=10',
                '-c:v', 'mjpeg', '-q:v', '10',
                '-an',
                '-f', 'mpjpeg', '-listen', '1', 'http://0.0.0.0:8080'
            ]
            try:
                mjpeg_proc = subprocess.Popen(cmd)
                mjpeg_proc.wait()
                logger.info("[MJPEG] Sesión cerrada. Reiniciando escucha...")
            except Exception as e:
                logger.error(f"Error: {e}")
        time.sleep(2)

def lanzar_fuente(origen, es_url=True):
    aniquilar("fuente")
    logger.info(f"[*] Lanzando fuente: {'Cámara' if es_url else 'Espera'}")
    cmd = ['ffmpeg', '-y', '-nostdin', '-loglevel', 'error']
    if es_url:
        cmd += ['-use_wallclock_as_timestamps', '1', '-rw_timeout', '5000000', '-i', origen, '-c:v', 'copy', '-an']
    else:
        cmd += ['-fflags', '+genpts', '-stream_loop', '-1', '-i', origen, '-c:v', 'copy', '-an']
    cmd += ['-f', 'mpegts', FIFO_PATH]
    PROCESOS["fuente"] = subprocess.Popen(cmd)

def main():
    logger.info("=== MIPC BRIDGE v31.10 THE INDEPENDENT DUAL ENGINE ===")
    subprocess.run("pkill -9 -f ffmpeg", shell=True, stderr=subprocess.DEVNULL)

    if os.path.exists(FIFO_PATH): os.remove(FIFO_PATH)
    os.mkfifo(FIFO_PATH)

    # 1. El Maestro arranca la señal principal
    iniciar_maestro()
    time.sleep(2)

    # 2. El servidor MJPEG corre en paralelo leyendo del Maestro
    mjpeg_thread = threading.Thread(target=loop_servidor_mjpeg, daemon=True)
    mjpeg_thread.start()

    global FIFO_KEEPER
    fifo_keeper = open(FIFO_PATH, 'wb')
    FIFO_KEEPER = fifo_keeper
    # Registrar manejadores de señal para shutdown ordenado
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    lanzar_fuente(PLACEHOLDER_PATH, es_url=False)
    is_camera_active = False

    while True:
        try:
            if PROCESOS["maestro"] is None or PROCESOS["maestro"].poll() is not None:
                iniciar_maestro()

            red_ok = check_port(CAM_IP, CAM_PORT)

            if is_camera_active:
                if PROCESOS["fuente"].poll() is not None or not red_ok:
                    logger.warning("[!] Cámara desconectada.")
                    lanzar_fuente(PLACEHOLDER_PATH, es_url=False)
                    is_camera_active = False
            else:
                if red_ok:
                    try:
                        client = MipcCameraClient(CAM_IP)
                        client.login(CAM_USER, CAM_PASS)
                        url = client.get_rtmp_stream()
                        if url:
                            logger.info("[*] SEÑAL OK.")
                            lanzar_fuente(url, es_url=True)
                            is_camera_active = True
                    except Exception as e:
                        logger.error(f"Error: {e}")
        except Exception as e:
            logger.error(f"Error: {e}")
        time.sleep(5)

if __name__ == "__main__":
    main()
