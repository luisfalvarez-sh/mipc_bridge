#!/bin/bash

echo "[$(date)] --- INICIANDO MIPC WORKER v16.3 (GPU ENABLED) ---"

# Verificar nodos críticos
DEVICES=("/dev/dri/card1" "/dev/video10" "/dev/fb0" "/dev/vcsm-cma" "/dev/vchiq")

for DEV in "${DEVICES[@]}"; do
    if [ -e "$DEV" ]; then
        echo "[OK] Detectado: $DEV"
        chmod 666 "$DEV" 2>/dev/null
    else
        echo "[ADVERTENCIA] No se encuentra $DEV. La GPU podría no funcionar."
    fi
done

# Ejecutar el puente
python3 -u /app/bridge.py
