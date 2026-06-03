#import hosts
from traffic import router as traffic_router
import ipaddress
import time
from typing import List, Dict, Any, Optional
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import requests
from requests.auth import HTTPBasicAuth

# =====================================================================
# CONFIGURACIÓN DE CONEXIÓN A NTOPNG
# =====================================================================
NTOPNG_BASE_URL = "http://195.0.4.100:3000"  # Cambia por la IP y puerto de tu ntopng
NTOPNG_USER = "admin"
NTOPNG_PASSWORD = "Kalapaucius0.123"

app = FastAPI(
    title="ntopng Custom Dashboard API Wrapper",
    description="Backend en Python para mapear y normalizar las APIs de ntopng para EPIC 1",
    version="1.0.0"
)

# Permitir conexiones desde tu Frontend (React, Vue, HTML/JS plano)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Cambiar por el dominio de tu frontend en producción
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(traffic_router)
#app.include_router(hosts.router, prefix="/api/v1")

# =====================================================================
# MODELOS DE DATOS (PYDANTIC) PARA TU FRONTEND
# =====================================================================

class HostItem(BaseModel):
    ip: str
    mac: str
    hostname: str
    vendor: str
    os: str
    is_online: bool
    uptime_seconds: int
    throughput_bps: float
    bytes_total: int

class US001Response(BaseModel):
    metrics: Dict[str, int]
    hosts: List[HostItem]

class HostDetailResponse(BaseModel):
    ip: str
    mac: str
    hostname: str
    vendor: str
    os: str
    is_online: bool
    bytes_sent: int
    bytes_rcvd: int
    packets_sent: int
    packets_rcvd: int
    active_flows: int
    l7_protocols: List[Dict[str, Any]]

# =====================================================================
# FUNCIONES AUXILIARES DE CONEXIÓN
# =====================================================================

def query_ntopng_api(endpoint: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Helper para autenticarse y consultar a ntopng gestionando errores."""
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
        
        # Estructura envolvente de ntopng estándar: { rc: 0, rc_str: "OK", rsp: {...} }
        if json_data.get("rc") == 0:
            return json_data.get("rsp", {})
        else:
            raise HTTPException(status_code=500, detail=f"ntopng Error: {json_data.get('rc_str')}")
            
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=503, detail=f"No se pudo conectar a ntopng: {str(e)}")

# =====================================================================
# US-001: LISTADO DE EQUIPOS LAN Y MÉTRICAS
# =====================================================================

@app.get("/api/v1/lan-hosts", response_model=US001Response)
def get_lan_hosts(
    ifid: int = Query(0, description="Interface ID de ntopng"),
    search: Optional[str] = Query(None, description="Búsqueda por IP, MAC o Hostname")
):
    """
    Obtiene la lista completa de hosts activos en la interfaz seleccionada,
    calcula métricas acumuladas y permite filtrado/búsqueda.
    """
    # Consulta a la API base de ntopng agregando el parámetro perPage para traer todos los hosts (hasta 5000)
    ntopng_rsp = query_ntopng_api(
        "/lua/rest/v2/get/host/active.lua", 
        params={"ifid": ifid, "perPage": 5000}
    )
    hosts_raw = ntopng_rsp.get("data", [])
    
    normalized_hosts: List[HostItem] = []
    
    # Contadores para las métricas sugeridas
    total_hosts = 0
    active_hosts = 0
    inactive_hosts = 0
    unknown_hosts = 0
    new_hosts = 0  # Lógica basada en tiempo de descubrimiento si aplica
    
    current_time = int(time.time())

    for host in hosts_raw:

        # Extraer campos base para determinar el estado online
        thpt_bps = host.get("thpt", {}).get("bps", 0.0)
        last_seen = host.get("last_seen", current_time)
        is_online = (current_time - last_seen < 60) or (thpt_bps > 0)

        # 1. FILTRO DE ESTADO ACTIVO (Saltar si el host está offline)
        if not is_online:
            continue

        # Obtener la IP del host de forma segura
        ip = host.get("ip") or host.get("key", "0.0.0.0").replace("__", ".")

        # 2. FILTRO POR RANGO DE IP (195.0.4.1 al 195.0.6.0)
        try:
            host_ip_obj = ipaddress.ip_address(ip)
            
            # CONTROL DE VERSIÓN: Si es IPv6, la ignoramos de inmediato
            if host_ip_obj.version != 4:
                continue
                
            ip_min = ipaddress.ip_address("195.0.4.1")
            ip_max = ipaddress.ip_address("195.0.8.0")
            
            # Si la IP está fuera de este rango IPv4, la ignoramos
            if not (ip_min <= host_ip_obj <= ip_max):
                continue
        except ValueError:
            # Si ntopng devuelve algo que no es una IP válida, lo salta
            continue

        # -----------------------------------------------------------------
        # Procesamiento y Normalización de Datos (Solo para IPs del rango)
        # -----------------------------------------------------------------
        mac = host.get("mac", "00:00:00:00:00:00")
        hostname = host.get("name") or host.get("hostname", "Desconocido")
        vendor = host.get("vendor", "Desconocido")
        os_detected = host.get("os", "Desconocido")
        
        # Calcular tiempo activo aproximado
        uptime = current_time - host.get("first_seen", current_time)
        bytes_total = host.get("bytes", {}).get("total", 0)

        # Filtrar si hay una búsqueda activa en el frontend (Barra de búsqueda)
        if search:
            search_lower = search.lower()
            if (search_lower not in ip.lower() and 
                search_lower not in mac.lower() and 
                search_lower not in hostname.lower()):
                continue

        # Clasificación para Métricas (Basado exclusivamente en tu rango objetivo)
        total_hosts += 1
        active_hosts += 1
            
        if hostname == "Desconocido" or vendor == "Desconocido":
            unknown_hosts += 1
            
        if (current_time - host.get("first_seen", current_time)) < 1800:
            new_hosts += 1

        # Mapear objeto normalizado al estándar esperado por el Front
        normalized_hosts.append(
            HostItem(
                ip=ip,
                mac=mac,
                hostname=hostname,
                vendor=vendor,
                os=str(os_detected),
                is_online=is_online,
                uptime_seconds=uptime,
                throughput_bps=thpt_bps,
                bytes_total=bytes_total
            )
        )

    return US001Response(
        metrics={
            "cantidad_total_hosts": total_hosts,
            "hosts_activos": active_hosts,
            "hosts_inactivos": inactive_hosts,
            "nuevos_hosts_detectados": new_hosts,
            "hosts_desconocidos": unknown_hosts
        },
        hosts=normalized_hosts
    )

# =====================================================================
# US-002: DETALLE INDIVIDUAL DE HOST
# =====================================================================

@app.get("/api/v1/hosts/{host_ip}", response_model=HostDetailResponse)
def get_host_detail(host_ip: str, ifid: int = Query(0, description="Interface ID de ntopng")):
    """
    Devuelve los datos avanzados de telemetría de un dispositivo específico,
    combinando los datos generales de red con sus estadísticas L7 (Protocolos).
    """
    # 1. Obtener datos crudos de red del Host
    host_data = query_ntopng_api("/lua/rest/v2/get/host/data.lua", params={"ifid": ifid, "host": host_ip})
    
    if not host_data:
        raise HTTPException(status_code=404, detail=f"Host {host_ip} no encontrado en ntopng")
        
    # 2. Obtener analíticas de protocolos Capa 7 (Aplicaciones como Netflix, DNS, SSH...)
    l7_data = query_ntopng_api("/lua/rest/v2/get/host/l7/stats.lua", params={"ifid": ifid, "host": host_ip})
    
    # Limpiar y estructurar protocolos L7 para gráficos (Ej: Pie charts)
    protocols_list = []
    if isinstance(l7_data, dict):
        # ntopng suele devolver pares clave-valor tipo {"HTTP": 145022, "DNS": 4122}
        for proto, bytes_value in l7_data.items():
            if isinstance(bytes_value, (int, float)) and bytes_value > 0:
                protocols_list.append({"protocol": proto, "bytes": bytes_value})
                
    # Ordenar protocolos de mayor a menor consumo
    protocols_list = sorted(protocols_list, key=lambda x: x["bytes"], reverse=True)

    # 3. Consolidación y normalización final del objeto
    current_time = int(time.time())
    last_seen = host_data.get("seen_last", current_time)

    return HostDetailResponse(
        ip=host_ip,
        mac=host_data.get("mac", "00:00:00:00:00:00"),
        hostname=host_data.get("name") or host_data.get("hostname", "Desconocido"),
        vendor=host_data.get("vendor", "Desconocido"),
        os=str(host_data.get("os", "Desconocido")),
        is_online=(current_time - last_seen < 60),
        bytes_sent=host_data.get("bytes.sent", 0),
        bytes_rcvd=host_data.get("bytes.rcvd", 0),
        packets_sent=host_data.get("packets.sent", 0),
        packets_rcvd=host_data.get("packets.rcvd", 0),
        active_flows=host_data.get("ndpi", {}).get("flows", 0),
        l7_protocols=protocols_list
    )

# =====================================================================
# EJECUCIÓN LOCAL
# =====================================================================
if __name__ == "__main__":
    import uvicorn
    # Corre el backend localmente en el puerto 8000
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)