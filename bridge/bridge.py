import os
import time
import subprocess
import errno
import sys
try:
    from bridge.process_manager import ProcessManager
except Exception:
    # Cuando el script se ejecuta como /app/bridge.py (no como paquete),
    # 'bridge' no es un paquete. Añadimos el directorio actual al sys.path
    # y probamos la importación local.
    _here = os.path.dirname(__file__)
    if _here not in sys.path:
        sys.path.insert(0, _here)
    from process_manager import ProcessManager
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
shutdown_event = threading.Event()

# Process manager
manager = None

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

FFMPEG_LOGLEVEL = os.getenv('FFMPEG_LOGLEVEL', 'error')
FFMPEG_MJPEG_LOGLEVEL = os.getenv('FFMPEG_MJPEG_LOGLEVEL', 'quiet')
FFMPEG_RW_TIMEOUT = os.getenv('FFMPEG_RW_TIMEOUT')

PROCESOS = {"maestro": None, "fuente": None, "recorder": None}

def _is_running(name):
    """Return True if process 'name' is currently running."""
    try:
        if manager is None:
            return False
        w = manager.get(name)
        if not w:
            return False
        # ProcessWrapper provides poll()
        return w.poll() is None
    except Exception:
        return False

def check_port(ip, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.2)
        return s.connect_ex((ip, port)) == 0

def _rtsp_ready():
    cmd = [
        'ffmpeg', '-y', '-nostdin', '-loglevel', 'error',
        '-rtsp_transport', 'tcp',
        '-i', RTSP_LOCAL, '-frames:v', '1', '-an', '-f', 'null', '-'
    ]
    try:
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        return res.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False

def _wait_rtsp_ready(max_wait_s=20, sleep_s=1):
    waited = 0
    while waited < max_wait_s and not shutdown_event.is_set():
        if check_port(RTSP_HOST, 8554) and _rtsp_ready():
            return True
        time.sleep(sleep_s)
        waited += sleep_s
    return False

def aniquilar(llave):
    global manager
    if manager:
        try:
            manager.stop(llave)
        except Exception as e:
            logger.error(f"Error al aniquilar {llave}: {e}")
    PROCESOS[llave] = None


def _shutdown(signum, frame):
    logger.info(f"Recibido señal {signum}, deteniendo procesos...")
    try:
        shutdown_event.set()
        aniquilar('fuente')
        aniquilar('maestro')
        aniquilar('recorder')
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
        'ffmpeg', '-y', '-nostdin', '-loglevel', FFMPEG_LOGLEVEL,
        '-fflags', '+genpts+igndts+flush_packets',
        '-f', 'mpegts', '-i', FIFO_PATH,
        '-c:v', 'copy',
        '-bsf:v', 'h264_mp4toannexb,dump_extra',
        '-c:a', 'copy',
        '-f', 'rtsp', '-rtsp_transport', 'tcp', RTSP_LOCAL
    ]
    w = manager.start('maestro', cmd)
    PROCESOS["maestro"] = w

def loop_servidor_mjpeg():
    """
    SERVIDOR MJPEG (Tablet Vieja):
    Lee del stream RTSP local generado por el Maestro.
    Si la tablet falla, este proceso se reinicia sin afectar al Maestro.
    """
    logger.info("[MJPEG] Servidor 8080 listo. Esperando conexión de tablet vieja...")
    while not shutdown_event.is_set():
        # Esperamos a que el RTSP local tenga señal antes de intentar codificar MJPEG
        if _wait_rtsp_ready(max_wait_s=10, sleep_s=1):
            cmd = [
                'ffmpeg', '-y', '-nostdin', '-loglevel', FFMPEG_MJPEG_LOGLEVEL,
                '-rtsp_transport', 'tcp',
                '-c:v', 'h264_v4l2m2m', '-i', RTSP_LOCAL,
                '-vf', 'scale=640:360,fps=10',
                '-c:v', 'mjpeg', '-q:v', '10',
                '-an',
                '-f', 'mpjpeg', '-listen', '1', 'http://0.0.0.0:8080'
            ]
            try:
                w = manager.start('mjpeg', cmd)
                # wait for process to finish or shutdown
                while w and w.poll() is None and not shutdown_event.is_set():
                    time.sleep(0.5)
                logger.info("[MJPEG] Sesión cerrada. Reiniciando escucha...")
                manager.stop('mjpeg')
            except Exception as e:
                logger.error(f"Error: {e}")
        time.sleep(2)

def lanzar_fuente(origen, es_url=True):
    aniquilar("fuente")
    logger.info(f"[*] Lanzando fuente: {'Cámara' if es_url else 'Espera'}")
    cmd = ['ffmpeg', '-y', '-nostdin', '-loglevel', FFMPEG_LOGLEVEL]
    if es_url:
        cmd += ['-use_wallclock_as_timestamps', '1']
        if FFMPEG_RW_TIMEOUT:
            cmd += ['-rw_timeout', str(FFMPEG_RW_TIMEOUT)]
        cmd += ['-i', origen, '-c:v', 'copy', '-an']
    else:
        # Use -re to read the placeholder at realtime and normalize timestamps
        cmd += ['-re', '-use_wallclock_as_timestamps', '1', '-fflags', '+genpts', '-stream_loop', '-1', '-i', origen, '-c:v', 'copy', '-an']
    cmd += ['-f', 'mpegts', FIFO_PATH]
    w = manager.start('fuente', cmd)
    PROCESOS["fuente"] = w

def main():
    logger.info("=== MIPC BRIDGE v31.10 THE INDEPENDENT DUAL ENGINE ===")
    # No matar procesos globalmente con pkill (podría afectar al host)

    global manager
    manager = ProcessManager(logger)

    # Crear FIFO
    try:
        if os.path.exists(FIFO_PATH):
            os.remove(FIFO_PATH)
        os.mkfifo(FIFO_PATH)
    except Exception as e:
        logger.error(f"No se pudo crear FIFO {FIFO_PATH}: {e}")
        sys.exit(1)

    # Intentar abrir descriptor en modo lectura/escritura no bloqueante para evitar bloqueos
    global FIFO_KEEPER
    try:
        fd = os.open(FIFO_PATH, os.O_RDWR | os.O_NONBLOCK)
        FIFO_KEEPER = os.fdopen(fd, 'wb')
    except OSError as e:
        logger.warning(f"No se pudo abrir FIFO en O_RDWR|O_NONBLOCK: {e}; intentando abrir en bucle")
        opened = False
        for _ in range(10):
            try:
                fd = os.open(FIFO_PATH, os.O_RDWR)
                FIFO_KEEPER = os.fdopen(fd, 'wb')
                opened = True
                break
            except OSError as e2:
                time.sleep(0.5)
        if not opened:
            logger.error("Fallo al abrir FIFO, abortando")
            sys.exit(1)

    # Registrar manejadores de señal para shutdown ordenado
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # 1. El Maestro arranca la señal principal
    iniciar_maestro()
    time.sleep(2)

    # 2. El servidor MJPEG corre en paralelo leyendo del Maestro
    mjpeg_thread = threading.Thread(target=loop_servidor_mjpeg, daemon=True)
    mjpeg_thread.start()
    # Iniciar recorder si está habilitado en .env
    try:
        GRABAR_VIDEO = os.getenv('GRABAR_VIDEO', 'false').lower() in ('1', 'true', 'yes')
    except Exception:
        GRABAR_VIDEO = False

    if GRABAR_VIDEO:
        def _start_recorder_when_ready():
            try:
                os.makedirs('/app/grabaciones', exist_ok=True)
            except Exception:
                logger.warning('No se pudo asegurar /app/grabaciones')

            seg_min = int(os.getenv('MINUTOS_SEGMENTO', '15'))
            seg_time = max(10, seg_min * 60)

            # Esperar hasta que RTSP del maestro esté disponible
            if not _wait_rtsp_ready(max_wait_s=30, sleep_s=1):
                logger.warning("[RECORDER] RTSP local no listo; abortando inicio de recorder")
                return

            if shutdown_event.is_set():
                return

            logger.info("[*] GRABAR_VIDEO habilitado, iniciando recorder desde RTSP local...")
            rec_cmd = [
                'ffmpeg', '-y', '-nostdin', '-loglevel', FFMPEG_LOGLEVEL,
                '-rtsp_transport', 'tcp', '-use_wallclock_as_timestamps', '1',
                '-i', RTSP_LOCAL,
                '-c', 'copy', '-map', '0',
                '-f', 'segment', '-segment_time', str(seg_time), '-segment_format', 'mpegts', '-strftime', '1',
                '/app/grabaciones/%Y%m%d-%H%M%S.ts'
            ]
            try:
                w = manager.start('recorder', rec_cmd)
                PROCESOS['recorder'] = w
            except Exception as e:
                logger.error(f"Error iniciando recorder: {e}")

        t = threading.Thread(target=_start_recorder_when_ready, daemon=True)
        t.start()

    lanzar_fuente(PLACEHOLDER_PATH, es_url=False)
    is_camera_active = False

    while True:
        try:
            if not _is_running('maestro'):
                iniciar_maestro()

            red_ok = check_port(CAM_IP, CAM_PORT)

            if is_camera_active:
                if (not _is_running('fuente')) or (not red_ok):
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
