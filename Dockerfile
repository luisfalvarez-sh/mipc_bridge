FROM python:3.11-slim-bookworm
ENV DEBIAN_FRONTEND=noninteractive

# 1. Instalamos dependencias de sistema
# Eliminamos 'ffplay' de la lista porque ya viene dentro de 'ffmpeg'
# Aseguramos espacios correctos entre paquetes
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    xserver-xorg-core \
    xserver-xorg-video-modesetting \
    x11vnc \
    libgl1-mesa-dri \
    libgles2-mesa \
    libdrm2 \
    v4l-utils \
    x11-xserver-utils \
    && echo "Instalación completada correctamente."

# 2. Instalamos librerías de Python
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# 3. Preparación de directorios
WORKDIR /app
RUN mkdir -p /app/config /app/logs /app/grabaciones

# 4. Copia de archivos (Asegúrate de tenerlos en la carpeta /opt/mipc-bridge/)
COPY bridge/bridge.py /app/bridge.py
COPY scripts/init_host.sh /app/init_host.sh
COPY config/xorg_gpu.conf /app/config/xorg_gpu.conf

# 5. Permisos y Punto de Entrada
RUN chmod +x /app/init_host.sh
ENTRYPOINT ["/app/init_host.sh"]
