# mipc-bridge

Proyecto "mipc-bridge": un puente ligero que conecta cámaras MIPC con un servidor de medios (mediamtx) y proporciona dos salidas principales:

- RTSP/HLS para clientes modernos (vía `mediamtx`).
- MJPEG/VNC para clientes/ tablets antiguas.

El componente principal es `bridge/bridge.py`, un worker en Python que usa `ffmpeg` para encaminar la señal (modo "maestro" y "fuente") y un cliente MIPC para obtener la URL de la cámara.

**Estado**: implementación funcional. Soporte para reconexión automática utilizando `assets/reconnecting.mp4` como fallback.

**Tabla de contenido**

- Resumen
- Requisitos
- Instalación (Docker)
- Ejecución y logs
- Configuración
- Estructura del proyecto
- Notas de seguridad
- Contribuir

## Requisitos

- Docker & Docker Compose (recomendado).
- Alternativamente: Python 3.11, `ffmpeg`, y los paquetes Python `requests` y `mipc-camera-client`.
- Para aceleración hardware (opcional) los dispositivos expuestos en `docker-compose.yml` (/dev/dri, /dev/video*, /dev/vcsm-cma, /dev/vchiq, /dev/fb0).

## Instalación (con Docker Compose)

Desde la raíz del proyecto:

```bash
# Construye y levanta los servicios (mediamtx + worker)
docker compose up -d --build

# Alternativa (sistemas con docker-compose instalable):
docker-compose up -d --build
```

Comprobación de logs:

```bash
docker compose logs -f bridge
# o
docker logs -f mipc_worker
```

## Ejecución local (no docker)

La ejecución está pensada para correr desde el contenedor (rutas interiores a `/app`). Ejecutar localmente requiere replicar la estructura `/app/config` y tener `ffmpeg` en PATH. Si decides ejecutar localmente:

```bash
python3 -m pip install --user requests mipc-camera-client
mkdir -p /app/config /app/grabaciones /app/logs
cp -r config/* /app/config/
python3 -u bridge/bridge.py
```

Nota: la forma más sencilla y reproducible es usar Docker Compose por las rutas y permisos.

## Configuración

La configuración principal ahora se obtiene a través de variables de entorno. Los parámetros obligatorios leídos por el worker son:

- `CAM_IP` (IP de la cámara)
- `CAM_USER` (usuario de la cámara)
- `CAM_PASS` (contraseña de la cámara)

Uso seguro de credenciales:

- Crea un archivo `.env` en la raíz con las credenciales y variables de entorno necesarias.

- `.env` está incluido en `.gitignore` para evitar que las credenciales se suban por accidente. No subas ese archivo al repositorio.

Ejemplo de variables para `.env`:

```ini
CAM_IP=192.168.1.100
CAM_USER=tu_usuario
CAM_PASS=tu_password
# Opcionales
CAM_PORT=7010
ENABLE_WEB=true
ENABLE_RTSP=true
ENABLE_VNC=false
RES_MAIN=1920x1080
GRABAR_VIDEO=false
DIAS_RETENCION=7
MINUTOS_SEGMENTO=15
TIMEZONE_OFFSET=-6
```

Importante: NO mantengas credenciales en repositorios públicos. Usa solo `.env` local y no lo subas al repositorio.

## Estructura del proyecto

- `docker-compose.yml` — despliegue de `mediamtx` + `bridge`.
- `Dockerfile` — imagen del worker en la raíz, para que la construcción sea explícita.
- `bridge/` — código del worker.
  - `bridge/bridge.py` — script principal del worker (gestiona ffmpeg, reconexión y servidor MJPEG).
- `scripts/` — scripts de arranque y utilidades (ej. `scripts/init_host.sh`).
- `config/` — configuración de Xorg (`config/xorg_gpu.conf`).
- `assets/` — recursos estáticos; aquí vive `reconnecting.mp4`.
- `recordings/` — grabaciones `.ts` en el host, montadas a `/app/grabaciones`.

## Cómo funciona (resumen técnico)

- El worker crea un FIFO (`/tmp/mipc_fifo`) donde `ffmpeg` escribe la fuente (`fuente`) y otro proceso (`maestro`) lee y publica por RTSP en `mediamtx`.
- Cuando la cámara no está disponible, se usa `assets/reconnecting.mp4` en loop como fuente de reemplazo.
- Un hilo paralelo ejecuta un servidor MJPEG (puerto 8080) que consume el RTSP local y lo sirve a clientes antiguos.

## Logs y depuración

- Salida principal disponible vía `docker logs mipc_worker` o `docker compose logs -f bridge`.
- El proyecto ya no mantiene una carpeta `logs/` versionada; si necesitas persistir logs, añade un volumen propio.

## Notas de seguridad y privacidad

- No comitees `.env` con credenciales. Usa `.gitignore` y mantenlo solo en tu entorno local.
- Limita el acceso a puertos expuestos y asegúrate de que la red donde reside la cámara sea segura.

Acción tomada en este repositorio:

- Se ha identificado `config/xorg_gpu.conf` bajo control de versiones; si ese fichero contiene credenciales o secretos, debe eliminarse de la rama `main`. Si ves que falta información sensible en otra ruta, elimina esos archivos locales y mantén las credenciales únicamente en `.env` (no versionado).
- Si prefieres que el historial público sea purgado de secretos, considera usar herramientas como `git filter-repo` o `git filter-branch` para reescribir el historial (esta operación es destructiva y requiere coordinación con colaboradores).
## Mejoras recomendadas

- Añadir tests básicos de integración y healthchecks para `mediamtx` y `bridge`.

Si has pedido limpiar la rama `main` de archivos con secretos, revisa que `CAM_PORT` y otras variables sensibles estén solo en `.env` local y no en el repositorio.
## Contribuir

Abre issues para errores y propuestas; los PRs son bienvenidos. Añade descripciones claras y pruebas cuando sea posible.

## Licencia

Sin licencia definida en el repo. Añade un archivo `LICENSE` si quieres publicar bajo una licencia específica.
