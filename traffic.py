import time
import ipaddress
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
import requests
from requests.auth import HTTPBasicAuth

# Configuración local al módulo para evitar dependencias circulares con main.py
NTOPNG_BASE_URL = "http://195.0.4.100:3000"
NTOPNG_USER = "admin"
NTOPNG_PASSWORD = "Kalapaucius0.123"

router = APIRouter(
    prefix="/api/v1/traffic",
    tags=["Monitoreo de Tráfico - EPIC 2"]
)

# =====================================================================
# MODELOS DE DATOS (PYDANTIC)
# =====================================================================

class HostTrafficDetail(BaseModel):
    ip: str
    hostname: str
    download_bytes: int
    upload_bytes: int
    total_bytes: int
    throughput_download_bps: float
    throughput_upload_bps: float
    throughput_total_bps: float
    active_sessions: int
    ratio_tx_rx: float
    traffic_internal_bytes: int
    traffic_external_bytes: int
    historical_points: List[Dict[str, Any]]

class ConsumerItem(BaseModel):
    ranking: int
    ip: str
    hostname: str
    vendor: str
    total_bytes: int
    throughput_bps: float
    is_online: bool

# =====================================================================
# HELPER INTERNO
# =====================================================================
def call_ntopng(endpoint: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = f"{NTOPNG_BASE_URL}{endpoint}"
    try:
        response = requests.get(
            url, 
            params=params, 
            auth=HTTPBasicAuth(NTOPNG_USER, NTOPNG_PASSWORD),
            timeout=5
        )
        response.raise_for_status()
        json_data = response.json()
        if json_data.get("rc") == 0:
            return json_data.get("rsp", {})
        raise HTTPException(status_code=500, detail=f"ntopng Error: {json_data.get('rc_str')}")
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=503, detail=f"Error de red en módulo tráfico: {str(e)}")

# =====================================================================
# US-003: TRÁFICO DE RED POR HOST INDIVIDUAL
# =====================================================================
@app_router := router.get("/host/{host_ip}", response_model=HostTrafficDetail)
def get_host_traffic_metrics(host_ip: str, ifid: int = Query(4, description="Interface ID")):
    """Devuelve métricas detalladas de consumo de ancho de banda y rendimiento para un host específico."""
    raw_data = call_ntopng("/lua/rest/v2/get/host/data.lua", params={"ifid": ifid, "host": host_ip})
    
    if not raw_data:
        raise HTTPException(status_code=404, detail=f"Métricas no disponibles para el host: {host_ip}")

    # Extraer bytes y throughput
    upload_bytes = raw_data.get("bytes.sent", 0)
    download_bytes = raw_data.get("bytes.rcvd", 0)
    total_bytes = upload_bytes + download_bytes

    thpt_up = float(raw_data.get("thpt.sent", 0.0))
    thpt_down = float(raw_data.get("thpt.rcvd", 0.0))
    thpt_total = float(raw_data.get("thpt", {}).get("bps", 0.0))
    
    active_sessions = raw_data.get("ndpi", {}).get("flows", 0)

    # Calcular Ratio TX/RX (Evitar división por cero)
    ratio = round(upload_bytes / download_bytes, 2) if download_bytes > 0 else float(upload_bytes)

    # Simulación/Cálculo de tráfico interno vs externo basado en ntopng local peers
    traffic_internal = raw_data.get("bytes.local", 0)
    traffic_external = max(0, total_bytes - traffic_internal)

    # Generación de puntos simulados para gráficos históricos (basados en el consumo actual)
    # ntopng requiere base de datos externa (InfluxDB) para histórico puro, simulamos la ventana de tiempo para el Front.
    now = int(time.time())
    mock_history = [
        {"timestamp": now - 60, "download_bps": thpt_down * 0.8, "upload_bps": thpt_up * 0.9},
        {"timestamp": now - 45, "download_bps": thpt_down * 1.1, "upload_bps": thpt_up * 0.7},
        {"timestamp": now - 30, "download_bps": thpt_down * 0.9, "upload_bps": thpt_up * 1.2},
        {"timestamp": now - 15, "download_bps": thpt_down * 1.3, "upload_bps": thpt_up * 0.9},
        {"timestamp": now, "download_bps": thpt_down, "upload_bps": thpt_up}
    ]

    return HostTrafficDetail(
        ip=host_ip,
        hostname=raw_data.get("name") or raw_data.get("hostname", "Desconocido"),
        download_bytes=download_bytes,
        upload_bytes=upload_bytes,
        total_bytes=total_bytes,
        throughput_download_bps=thpt_down,
        throughput_upload_bps=thpt_up,
        throughput_total_bps=thpt_total,
        active_sessions=active_sessions,
        ratio_tx_rx=ratio,
        traffic_internal_bytes=traffic_internal,
        traffic_external_bytes=traffic_external,
        historical_points=mock_history
    )

# =====================================================================
# US-004: TOP CONSUMIDORES DE RED (RANKING)
# =====================================================================
@router.get("/top-consumers", response_model=List[ConsumerItem])
def get_top_consumers(
    ifid: int = Query(4, description="Interface ID de ntopng"),
    limit: int = Query(10, ge=1, le=50, description="Cantidad de hosts a retornar")
):
    """Devuelve el ranking de los hosts de la LAN con mayor volumen de tráfico acumulado."""
    raw_hosts = call_ntopng("/lua/rest/v2/get/host/active.lua", params={"ifid": ifid, "perPage": 5000})
    hosts_list = raw_hosts.get("data", [])

    processed_consumers = []
    
    # Límites de tu rango físico LAN
    ip_min = ipaddress.ip_address("195.0.4.1")
    ip_max = ipaddress.ip_address("195.0.6.0")

    current_time = int(time.time())

    for host in hosts_list:
        ip_str = host.get("ip") or host.get("key", "0.0.0.0").replace("__", ".")
        
        # Validar Filtro de Rango IP (Mismo criterio exitoso de la US-001)
        try:
            ip_obj = ipaddress.ip_address(ip_str)
            if ip_obj.version != 4 or not (ip_min <= ip_obj <= ip_max):
                continue
        except ValueError:
            continue

        # Datos de Consumo
        total_bytes = host.get("bytes", {}).get("total", 0)
        thpt_bps = host.get("thpt", {}).get("bps", 0.0)
        
        last_seen = host.get("last_seen", current_time)
        is_online = (current_time - last_seen < 60) or (thpt_bps > 0)

        processed_consumers.append({
            "ip": ip_str,
            "hostname": host.get("name") or host.get("hostname", "Desconocido"),
            "vendor": host.get("vendor", "Desconocido"),
            "total_bytes": total_bytes,
            "throughput_bps": thpt_bps,
            "is_online": is_online
        })

    # Ordenar de mayor a menor consumo de Bytes totales
    sorted_consumers = sorted(processed_consumers, key=lambda x: x["total_bytes"], reverse=True)

    # Tomar solo el límite solicitado (Top 10 o Top 20) y estructurar respuesta con posición de ranking
    final_ranking = []
    for index, item in enumerate(sorted_consumers[:limit]):
        final_ranking.append(
            ConsumerItem(
                ranking=index + 1,
                ip=item["ip"],
                hostname=item["hostname"],
                vendor=item["vendor"],
                total_bytes=item["total_bytes"],
                throughput_bps=item["throughput_bps"],
                is_online=item["is_online"]
            )
        )

    return final_ranking