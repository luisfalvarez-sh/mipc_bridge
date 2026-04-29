# mipc-bridge

mipc-bridge es un puente ligero para integrar cámaras MIPC con un servidor de medios (`mediamtx`) y servir la señal a clientes modernos y antiguos.

Este proyecto está adaptado para Raspberry Pi 4, corriendo PXVIRT (Proxmox) y Docker sobre LXC. 

Salidas principales:

- RTSP/HLS para clientes modernos (vía `mediamtx`).
- MJPEG (HTTP) para tablets/cliente antiguos (puerto 8080).

Componentes clave:

- `bridge/bridge.py`: worker principal en Python — gestiona procesos ffmpeg, reconexión, FIFO y grabación.
- `bridge/process_manager.py`: gestor de subprocesos seguro (start/stop, drenado de pipes, terminación por pgid).
- `docker-compose.yml`: define servicios `mediamtx` y `bridge`.
- `assets/reconnecting.mp4`: vídeo fallback cuando la cámara no está disponible.
- `recordings/` y `logs/`: mounts en host para segmentos y logs.

Estado: funcional — contiene mejoras de estabilidad aplicadas en Abril 2026 (ProcessManager, comprobación RTSP antes de arrancar MJPEG/recorder, opciones configurables de ffmpeg).

## Requisitos

- Docker & Docker Compose (recomendado).
- ffmpeg (incluido en la imagen Docker).
- Para ejecución local: Python 3.11 y `mipc-camera-client`.

## Despliegue con Docker Compose

Desde la raíz del proyecto:

```bash
docker compose up -d --build
```

Ver logs:

```bash
docker compose logs -f bridge
docker logs -f mipc_worker
```

## Variables de configuración (.env)

Crear `.env` en la raíz con al menos las credenciales de la cámara. Ejemplo mínimo:

```ini
CAM_IP=10.10.10.110
CAM_USER=usuario
CAM_PASS=contraseña
CAM_PORT=7010
GRABAR_VIDEO=true
MINUTOS_SEGMENTO=15
FFMPEG_LOGLEVEL=error
FFMPEG_MJPEG_LOGLEVEL=quiet
FFMPEG_RW_TIMEOUT=5000000  # opcional: sólo si tu build de ffmpeg lo soporta
```

Notas:

- `GRABAR_VIDEO=true` habilita el `recorder` (segmentación en `/app/grabaciones`).
- `FFMPEG_LOGLEVEL` y `FFMPEG_MJPEG_LOGLEVEL` pueden setearse a `info` o `debug` para diagnóstico.

## Flujo de trabajo y arquitectura

- El worker crea `/tmp/mipc_fifo`.
- `fuente` (ffmpeg) toma RTMP/RTSP o `assets/reconnecting.mp4` y escribe MPEG-TS al FIFO.
- `maestro` (ffmpeg) lee del FIFO y publica por RTSP en `mediamtx`.
- `mjpeg` (ffmpeg) lee el RTSP local y sirve MJPEG en `http://0.0.0.0:8080`.
- `recorder` (opcional) graba segmentos `.ts` en `/app/grabaciones`.

El `ProcessManager` lanza procesos en nuevos grupos, drena stdout/stderr y permite terminación limpia por `pgid`.

## Logs y grabaciones

- Logs del worker en host: `./logs/bridge.log` (rotating file handler).
- Grabaciones en host: `./recordings/*.ts`.

## Diagnóstico rápido

- Errores como `non-existing PPS 0 referenced` o `decode_slice_header error` suelen indicar frames dañados en la fuente RTMP; prueba a capturar 5s desde dentro del contenedor para validar:

```bash
docker compose exec -T bridge bash -lc "python - <<'PY'
from mipc_camera_client import MipcCameraClient
import os
c = MipcCameraClient(os.getenv('CAM_IP'))
c.login(os.getenv('CAM_USER'), os.getenv('CAM_PASS'))
print(c.get_rtmp_stream())
PY"

docker compose exec -T bridge ffmpeg -y -nostdin -loglevel error -i rtmp://***REDACTED*** -t 5 -c copy /tmp/rtmp_test.ts && ls -l /tmp/rtmp_test.ts
```

- Para más información, sube `FFMPEG_LOGLEVEL=info` y reinicia.

## Buenas prácticas

- No subir `.env` con credenciales. Mantén un `.env.example` sin secretos.
- Considera ejecutar el contenedor como usuario no-root en producción.

## Contribuir

- Abre issues con descripción y logs. Los PRs deben incluir pruebas o pasos de validación.

---
Actualizado: Abril 2026 — incluye mejoras de estabilidad y diagnóstico.
