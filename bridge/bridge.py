import os
import time
import subprocess
import errno
import sys
_here = os.path.dirname(os.path.abspath(__file__))
_parent = os.path.dirname(_here)
if _parent not in sys.path:
    sys.path.insert(0, _parent)
if _here not in sys.path:
    sys.path.insert(0, _here)

try:
    from bridge.process_manager import ProcessManager
except ImportError:
    from process_manager import ProcessManager
import socket
import http.server
import socketserver
import logging
import threading
import signal
from logging.handlers import RotatingFileHandler
from mipc_camera_client import MipcCameraClient

# Guardamos el descriptor del FIFO para poder cerrarlo en el shutdown
FIFO_KEEPER = None
shutdown_event = threading.Event()

# Process manager
manager = None

# ==========================================
#      CONFIGURACIÓN Y VARIABLES DE ENTORNO
# ==========================================
CONFIG_DIR = "/app/config"
CONFIG_ENV = os.getenv("CONFIG_ENV", "/app/.env")
FIFO_PATH = "/tmp/mipc_fifo"
PLACEHOLDER_PATH = "/app/assets/reconnecting.mp4"

def reload_env():
    """Carga o recarga el archivo .env si existe, sobrescribiendo variables en os.environ."""
    candidates = [CONFIG_ENV, "./.env", "/app/.env", os.path.join(os.path.dirname(__file__), "../.env")]
    for path in candidates:
        if os.path.exists(path):
            try:
                from dotenv import load_dotenv
                load_dotenv(path, override=True)
                break
            except Exception as e:
                pass

def get_env_var(key, default=None):
    """Obtiene una variable de entorno limpiando comentarios en línea (#...) y espacios en blanco."""
    val = os.getenv(key)
    if val is None:
        return default
    val = val.split('#')[0].strip()
    return val if val != "" else default

def get_env_bool(key, default=False):
    """Obtiene una variable de entorno parseada limpia como booleano."""
    val = get_env_var(key)
    if val is None:
        return default
    return val.lower() in ('1', 'true', 'yes', 'on')

def load_setting(key, default=None, mandatory=False):
    val = get_env_var(key, default)
    if mandatory and (val is None or val == ""):
        print(f"Missing mandatory config '{key}'", file=sys.stderr)
        sys.exit(1)
    return val

# Cargar .env antes de inicializar el sistema de logs
reload_env()

# ==========================================
#      SISTEMA DE LOGS CON ROTACIÓN
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("MIPC_BRIDGE")

try:
    log_dir = get_env_var('LOG_DIR', '/app/logs')
    os.makedirs(log_dir, exist_ok=True)
    
    try:
        max_bytes_mb = float(get_env_var('LOG_MAX_SIZE_MB', '5'))
    except (ValueError, TypeError):
        max_bytes_mb = 5.0
    
    try:
        backup_count = int(get_env_var('LOG_BACKUP_COUNT', '3'))
    except (ValueError, TypeError):
        backup_count = 3

    max_bytes = int(max_bytes_mb * 1024 * 1024)
    log_file_path = os.path.join(log_dir, 'bridge.log')

    fh = RotatingFileHandler(log_file_path, maxBytes=max_bytes, backupCount=backup_count)
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
    logger.addHandler(fh)
except Exception as e:
    logger.debug(f'No se pudo crear RotatingFileHandler en /app/logs: {e}')

def limpiar_logs_antiguos():
    """Elimina archivos de logs rotados antiguos en /app/logs si superan LOG_RETENTION_DAYS."""
    try:
        dias_str = get_env_var('LOG_RETENTION_DAYS', get_env_var('DIAS_RETENCION', '7'))
        dias = int(dias_str)
        if dias <= 0:
            return
        log_dir = get_env_var('LOG_DIR', '/app/logs')
        if not os.path.exists(log_dir):
            return

        limite_sec = time.time() - (dias * 86400)
        borrados = 0
        for fname in os.listdir(log_dir):
            if fname.startswith('bridge.log.') or (fname.endswith('.log') and fname != 'bridge.log'):
                fpath = os.path.join(log_dir, fname)
                try:
                    if os.path.isfile(fpath) and os.path.getmtime(fpath) < limite_sec:
                        os.remove(fpath)
                        borrados += 1
                except Exception as e:
                    logger.warning(f"No se pudo eliminar log antiguo {fpath}: {e}")
        if borrados > 0:
            logger.info(f"[*] Retención de logs ({dias} días): {borrados} archivo(s) de log rotado(s) eliminado(s).")
    except Exception as e:
        logger.error(f"Error en limpieza de logs por retención: {e}")

def limpiar_grabaciones_antiguas():
    """Elimina archivos .ts en /app/grabaciones con antigüedad mayor a DIAS_RETENCION."""
    try:
        dias_str = get_env_var('DIAS_RETENCION', '7')
        dias = int(dias_str)
        if dias <= 0:
            return
        grabaciones_dir = '/app/grabaciones'
        if not os.path.exists(grabaciones_dir):
            return

        limite_sec = time.time() - (dias * 86400)
        borrados = 0
        for fname in os.listdir(grabaciones_dir):
            if fname.endswith('.ts'):
                fpath = os.path.join(grabaciones_dir, fname)
                try:
                    if os.path.isfile(fpath) and os.path.getmtime(fpath) < limite_sec:
                        os.remove(fpath)
                        borrados += 1
                except Exception as e:
                    logger.warning(f"No se pudo eliminar grabacion antigua {fpath}: {e}")
        if borrados > 0:
            logger.info(f"[*] Retención ({dias} días): {borrados} archivo(s) antiguo(s) eliminado(s).")
    except Exception as e:
        logger.error(f"Error en limpieza de grabaciones por retención: {e}")

CAM_IP   = load_setting('CAM_IP', mandatory=True)
CAM_USER = load_setting('CAM_USER', mandatory=True)
CAM_PASS = load_setting('CAM_PASS', mandatory=True)
CAM_PORT = int(load_setting('CAM_PORT', default=7010))

RTSP_HOST = "mediamtx"
RTSP_LOCAL = f"rtsp://{RTSP_HOST}:8554/1"

FFMPEG_LOGLEVEL = get_env_var('FFMPEG_LOGLEVEL', 'error')
FFMPEG_MJPEG_LOGLEVEL = get_env_var('FFMPEG_MJPEG_LOGLEVEL', 'quiet')
FFMPEG_RW_TIMEOUT = get_env_var('FFMPEG_RW_TIMEOUT')

PROCESOS = {"maestro": None, "fuente": None, "recorder": None, "mjpeg": None}

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

# ==========================================
#  SERVIDOR HTTP MJPEG MULTICLIENTE ON-DEMAND (v31.10)
# ==========================================
LATEST_JPEG_FRAME = None
ACTIVE_CLIENTS = 0
FRAME_LOCK = threading.Lock()
CLIENT_LOCK = threading.Lock()

def _ffmpeg_mjpeg_generator():
    """Hilo de fondo que ejecuta FFmpeg On-Demand sólo cuando hay clientes activos."""
    global LATEST_JPEG_FRAME
    logger.info("[MJPEG-Gen] Generador de fotogramas On-Demand listo.")

    while not shutdown_event.is_set():
        with CLIENT_LOCK:
            has_clients = ACTIVE_CLIENTS > 0

        if not has_clients:
            time.sleep(1)
            continue

        if not _wait_rtsp_ready(max_wait_s=5, sleep_s=0.5):
            time.sleep(2)
            continue

        reload_env()
        res = get_env_var('MJPEG_RES', '640x360')
        fps = get_env_var('MJPEG_FPS', '10')
        quality = get_env_var('MJPEG_QUALITY', '8')
        scale_filter = res.replace('x', ':') if 'x' in res else res

        # fps antes de scale para reducir trabajo de escalado al 33% + fast_bilinear
        cmd = [
            'ffmpeg', '-y', '-nostdin', '-loglevel', FFMPEG_MJPEG_LOGLEVEL,
            '-rtsp_transport', 'tcp',
            '-i', RTSP_LOCAL,
            '-vf', f'fps={fps},scale={scale_filter}:flags=fast_bilinear',
            '-c:v', 'mjpeg', '-q:v', str(quality),
            '-an',
            '-f', 'image2pipe', '-'
        ]
        proc = None
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True
            )
            buffer = bytearray()
            logger.info(f"[MJPEG-Gen] Activando FFmpeg On-Demand (Res: {res}, FPS: {fps}, Quality: {quality})...")

            while not shutdown_event.is_set() and proc.poll() is None:
                with CLIENT_LOCK:
                    if ACTIVE_CLIENTS == 0:
                        logger.info("[MJPEG-Gen] 0 clientes activos. Deteniendo FFmpeg para ahorrar CPU.")
                        break

                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                buffer.extend(chunk)
                while True:
                    a = buffer.find(b'\xff\xd8')
                    b = buffer.find(b'\xff\xd9')
                    if a != -1 and b != -1 and b > a:
                        jpg = bytes(buffer[a:b+2])
                        buffer = buffer[b+2:]
                        with FRAME_LOCK:
                            LATEST_JPEG_FRAME = jpg
                    else:
                        break

        except Exception as e:
            logger.error(f"[MJPEG-Gen] Error en subproceso FFmpeg: {e}")
        finally:
            if proc:
                try:
                    stderr_out = ""
                    if proc.stderr:
                        stderr_out = proc.stderr.read().decode(errors='ignore')
                    proc.terminate()
                    proc.wait(timeout=2)
                    if proc.returncode not in (0, None, -15, -9) and stderr_out:
                        logger.warning(f"[MJPEG-Gen] FFmpeg finalizó con código {proc.returncode}: {stderr_out.strip()[:200]}")
                except Exception:
                    pass

        # PREVENIR SPIN-LOOP: siempre dormir al menos 2 segundos si el proceso finaliza
        time.sleep(2)

class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

class MJPEGRequestHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Desactivar logs verbosos de cada petición HTTP
        pass

    def do_HEAD(self):
        self.send_response(200)
        self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, pre-check=0, post-check=0, max-age=0')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Connection', 'close')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()

    def do_GET(self):
        global ACTIVE_CLIENTS
        with CLIENT_LOCK:
            ACTIVE_CLIENTS += 1

        self.send_response(200)
        self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, pre-check=0, post-check=0, max-age=0')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Connection', 'close')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()

        try:
            update_interval = float(get_env_var('MJPEG_UPDATE_INTERVAL', '0.05'))
        except (ValueError, TypeError):
            update_interval = 0.05

        last_sent = None
        try:
            while not shutdown_event.is_set():
                with FRAME_LOCK:
                    frame = LATEST_JPEG_FRAME
                if frame and frame != last_sent:
                    try:
                        self.wfile.write(b'--frame\r\n')
                        self.wfile.write(b'Content-Type: image/jpeg\r\n')
                        self.wfile.write(f'Content-Length: {len(frame)}\r\n\r\n'.encode('ascii'))
                        self.wfile.write(frame)
                        self.wfile.write(b'\r\n')
                        self.wfile.flush()
                        last_sent = frame
                    except (BrokenPipeError, ConnectionResetError, socket.error):
                        break
                time.sleep(update_interval)
        finally:
            with CLIENT_LOCK:
                ACTIVE_CLIENTS = max(0, ACTIVE_CLIENTS - 1)

def loop_servidor_mjpeg():
    """
    SERVIDOR MJPEG MULTICLIENTE (Android / Tablets Viejas / TinyCam):
    Lanza el generador de fotogramas en segundo plano y atiende múltiples clientes HTTP simultáneamente.
    """
    gen_thread = threading.Thread(target=_ffmpeg_mjpeg_generator, daemon=True)
    gen_thread.start()

    logger.info("[MJPEG] Servidor HTTP Multicliente listo en http://0.0.0.0:8080")
    try:
        server = ThreadedHTTPServer(('0.0.0.0', 8080), MJPEGRequestHandler)
        server.timeout = 1.0
        while not shutdown_event.is_set():
            server.handle_request()
        server.server_close()
    except Exception as e:
        logger.error(f"[MJPEG] Error en servidor HTTP: {e}")

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
        # Use -re and +genpts to loop placeholder video seamlessly with monotonic timestamps
        cmd += ['-re', '-stream_loop', '-1', '-i', origen, '-fflags', '+genpts+igndts', '-c:v', 'copy', '-an']
    cmd += ['-f', 'mpegts', FIFO_PATH]
    w = manager.start('fuente', cmd)
    PROCESOS["fuente"] = w

    # Iniciar / reiniciar Maestro para enganchar los datos frescos del FIFO
    time.sleep(1)
    iniciar_maestro()

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

    # 1. El servidor MJPEG corre en paralelo
    mjpeg_thread = threading.Thread(target=loop_servidor_mjpeg, daemon=True)
    mjpeg_thread.start()
    # Iniciar recorder si está habilitado en .env
    reload_env()
    GRABAR_VIDEO = get_env_bool('GRABAR_VIDEO', default=False)

    if GRABAR_VIDEO:
        def _start_recorder_when_ready():
            try:
                os.makedirs('/app/grabaciones', exist_ok=True)
            except Exception:
                logger.warning('No se pudo asegurar /app/grabaciones')

            seg_min = int(get_env_var('MINUTOS_SEGMENTO', '15'))
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
    else:
        logger.info("[*] GRABAR_VIDEO desactivado en .env. El recorder NO se iniciará.")
        aniquilar('recorder')

    # Ejecutar limpieza de grabaciones y logs por retención
    limpiar_grabaciones_antiguas()
    limpiar_logs_antiguos()

    lanzar_fuente(PLACEHOLDER_PATH, es_url=False)
    is_camera_active = False

    loop_count = 0
    while True:
        try:
            loop_count += 1
            if loop_count % 720 == 0:
                limpiar_grabaciones_antiguas()
                limpiar_logs_antiguos()

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
