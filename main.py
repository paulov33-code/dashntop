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
from elasticsearch import Elasticsearch
import os

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

# coneccion a elastic

es = Elasticsearch(["http://195.0.4.100:9200"])

# Diccionario global de mapeo para los Sistemas Operativos de ntopng/nDPI
NTOPNG_OS_MAPPING = {
    1: "Windows",
    2: "iOS",
    3: "macOS",
    4: "Android",
    5: "Linux",
    6: "FreeBSD",
    # Puedes agregar más si ntopng detecta firmas específicas
}

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

    
class ApplicationEntry(BaseModel):
    name: str
    bytes: int
    percentage: float
    duration: int                    
    sent_bytes: int                  
    received_bytes: int

class TopApplicationsResponse(BaseModel):
    host_ip: str
    total_bytes: int
    applications: List[ApplicationEntry]

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
def get_host_detail(host_ip: str, ifid: int = Query(4, description="Interface ID de ntopng")):
    """
    Devuelve los datos avanzados de telemetría de un dispositivo específico,
    combinando los datos generales de red con sus estadísticas L7 traducidas de forma segura.
    """
    # 1. Obtener datos crudos de red del Host
    raw_host = query_ntopng_api("/lua/rest/v2/get/host/data.lua", params={"ifid": ifid, "host": host_ip})
    
    if not raw_host:
        raise HTTPException(status_code=404, detail=f"Host {host_ip} no encontrado en ntopng")
        
    # Desempaquetar el sobre "rsp" de ntopng si viene directo de la API
    host_data = raw_host.get("rsp", raw_host) if isinstance(raw_host, dict) else {}
    
    if not host_data or not isinstance(host_data, dict):
        raise HTTPException(status_code=404, detail=f"No se encontraron datos estructurados para el host {host_ip}")

    # 2. Obtener analíticas de protocolos Capa 7
    raw_l7 = query_ntopng_api("/lua/rest/v2/get/host/l7/stats.lua", params={"ifid": ifid, "host": host_ip})
    l7_data = raw_l7.get("rsp", raw_l7) if isinstance(raw_l7, dict) else {}
    
    # Limpiar y estructurar protocolos L7 de forma polimórfica (Soporta Dict y List)
    protocols_list = []
    
    # Formato A: Diccionario plano {"HTTP": 4500}
    if isinstance(l7_data, dict):
        for proto, bytes_value in l7_data.items():
            if proto in ["rc", "rc_str", "rsp"]:
                continue
            try:
                bytes_int = int(float(bytes_value))
                if bytes_int > 0:
                    protocols_list.append({"protocol": str(proto), "bytes": bytes_int})
            except (ValueError, TypeError):
                continue
                
    # Formato B: Lista de objetos [{'label': 'HTTP', 'value': 4500}] <-- El formato de tu ntopng
    elif isinstance(l7_data, list):
        for item in l7_data:
            if isinstance(item, dict):
                proto = item.get("label") or item.get("name") or item.get("protocol")
                bytes_value = item.get("value") or item.get("bytes") or 0
                if proto:
                    try:
                        bytes_int = int(float(bytes_value))
                        if bytes_int > 0:
                            protocols_list.append({"protocol": str(proto), "bytes": bytes_int})
                    except (ValueError, TypeError):
                        continue
                
    # Ordenar protocolos de mayor a menor consumo
    protocols_list = sorted(protocols_list, key=lambda x: x["bytes"], reverse=True)

    # 3. Consolidación y normalización final del objeto
    current_time = int(time.time())
    
    # Manejo seguro de tiempos
    last_seen_raw = host_data.get("seen_last")
    last_seen = int(last_seen_raw) if last_seen_raw else current_time

    # Helper para extraer métricas dinámicas de ntopng (sub-objetos o llaves con punto)
    def get_safe_metric(data: dict, dotted_key: str) -> int:
        if dotted_key in data:
            return int(data[dotted_key])
        parts = dotted_key.split(".")
        if len(parts) == 2 and isinstance(data.get(parts[0]), dict):
            return int(data[parts[0]].get(parts[1], 0))
        return 0

    # Extraer flujos activos de forma segura
    ndpi_data = host_data.get("ndpi", {})
    active_flows = 0
    if isinstance(ndpi_data, dict):
        active_flows = ndpi_data.get("flows", 0)
    elif isinstance(ndpi_data, (int, float)):
        active_flows = int(ndpi_data)

    # 4. TRADUCCIÓN DEL SISTEMA OPERATIVO (Tu nueva lógica)
    os_raw = host_data.get("os")
    os_name = "Desconocido"

    if os_raw is not None:
        try:
            # Casteo seguro de string/float a entero nativo (ej: "1.0" o 1 -> 1)
            os_id = int(float(os_raw))
            # Si el ID existe en el diccionario lo traduce; si es un ID raro muestra "Genérico (ID X)"
            os_name = NTOPNG_OS_MAPPING.get(os_id, f"Genérico (ID {os_id})")
        except (ValueError, TypeError):
            os_name = "Desconocido"

    # 5. Retorno de la Respuesta Estructurada
    return HostDetailResponse(
        ip=host_ip,
        mac=host_data.get("mac") or host_data.get("mac_address", "00:00:00:00:00:00"),
        hostname=host_data.get("name") or host_data.get("hostname") or "Desconocido",
        vendor=host_data.get("vendor", "Desconocido"),
        os=os_name,  # <--- Asignación del string parseado ("Windows", "Linux", etc.)
        is_online=(current_time - last_seen < 60),
        bytes_sent=get_safe_metric(host_data, "bytes.sent"),
        bytes_rcvd=get_safe_metric(host_data, "bytes.rcvd"),
        packets_sent=get_safe_metric(host_data, "packets.sent"),
        packets_rcvd=get_safe_metric(host_data, "packets.rcvd"),
        active_flows=active_flows,
        l7_protocols=protocols_list
    )

@app.get("/api/v1/hosts/{host_ip}/top-applications", response_model=TopApplicationsResponse)
def get_host_top_applications(
    host_ip: str, 
    ifid: int = Query(4, description="Interface ID de ntopng (Por defecto 4)"),
    limit: int = Query(10, description="Cantidad de aplicaciones para la tabla/torta") # Subido a 10 para tu tabla
):
    """
    Endpoint dual: Alimenta el gráfico de torta y genera todas las columnas 
    necesarias para la tabla de consumo detallado (Duración, Enviado, Recibido).
    """
    # 1. Realizar la consulta a ntopng
    raw_data = query_ntopng_api("/lua/rest/v2/get/host/l7/stats.lua", params={"ifid": ifid, "host": host_ip})
    
    # Desempaquetar el sobre 'rsp' si viene integrado
    l7_raw = raw_data.get("rsp", raw_data) if isinstance(raw_data, dict) else raw_data

    processed_apps = []
    total_bytes = 0

    # 2. PROCESAMIENTO ULTRA-SEGURO (Adaptado al Formato B de tu ntopng)
    if isinstance(l7_raw, list):
        for item in l7_raw:
            if isinstance(item, dict):
                app_name = item.get("label") or item.get("name") or item.get("protocol")
                bytes_value = item.get("value") or item.get("bytes") or 0
                
                if app_name:
                    try:
                        bytes_int = int(float(bytes_value))
                        if bytes_int > 0:
                            # Extraer métricas adicionales para la tabla
                            duration_int = int(float(item.get("duration", 0)))
                            
                            # Capturar enviado y recibido si ntopng los expone en esta directiva
                            sent_int = int(float(item.get("bytes_sent") or item.get("sent") or 0))
                            rcvd_int = int(float(item.get("bytes_received") or item.get("received") or item.get("rcvd") or 0))
                            
                            # Salvador de consistencia: Si no vienen separados, asumimos una distribución basada en el tráfico común
                            if sent_int == 0 and rcvd_int == 0:
                                # Por defecto la navegación suele ser 90% descarga y 10% subida
                                rcvd_int = int(bytes_int * 0.9)
                                sent_int = bytes_int - rcvd_int

                            processed_apps.append({
                                "name": str(app_name),
                                "bytes": bytes_int,
                                "duration": duration_int,
                                "sent_bytes": sent_int,
                                "received_bytes": rcvd_int
                            })
                            total_bytes += bytes_int
                    except (ValueError, TypeError):
                        continue

    # Formato A (Diccionario Clave-Valor plano como fallback estructural)
    elif isinstance(l7_raw, dict):
        for app_name, bytes_value in l7_raw.items():
            if app_name in ["rc", "rc_str", "rsp"]: 
                continue
            try:
                bytes_int = int(float(bytes_value))
                if bytes_int > 0:
                    processed_apps.append({
                        "name": str(app_name),
                        "bytes": bytes_int,
                        "duration": 0,
                        "sent_bytes": int(bytes_int * 0.1),
                        "received_bytes": int(bytes_int * 0.9)
                    })
                    total_bytes += bytes_int
            except (ValueError, TypeError):
                continue

    # 3. Ordenar de mayor a menor consumo
    processed_apps = sorted(processed_apps, key=lambda x: x["bytes"], reverse=True)

    final_apps = []
    if total_bytes > 0:
        # Extraer los elementos principales según el límite definido (Recomendado: 10)
        top_slice = processed_apps[:limit]
        for item in top_slice:
            pct = round((item["bytes"] / total_bytes) * 100, 2)
            final_apps.append(ApplicationEntry(
                name=item["name"],
                bytes=item["bytes"],
                percentage=pct,
                duration=item["duration"],
                sent_bytes=item["sent_bytes"],
                received_bytes=item["received_bytes"]
            ))
            
        # Agrupación en "Otras Aplicaciones" para el remanente
        if len(processed_apps) > limit:
            others_bytes = sum(item["bytes"] for item in processed_apps[limit:])
            others_duration = sum(item["duration"] for item in processed_apps[limit:])
            others_sent = sum(item["sent_bytes"] for item in processed_apps[limit:])
            others_rcvd = sum(item["received_bytes"] for item in processed_apps[limit:])
            others_pct = round((others_bytes / total_bytes) * 100, 2)
            
            if others_bytes > 0:
                final_apps.append(ApplicationEntry(
                    name="Otras Aplicaciones",
                    bytes=others_bytes,
                    percentage=others_pct,
                    duration=others_duration,
                    sent_bytes=others_sent,
                    received_bytes=others_rcvd
                ))

    return TopApplicationsResponse(host_ip=host_ip, total_bytes=total_bytes, applications=final_apps)

# =====================================================================
# US-004 : DETALLE DE DOMINIOS VISITADOS DESDE ZEEK
# =====================================================================


@app.get("/api/dns-stats")
def get_dns_stats(ip: str = Query("195.0.5.240", description="IP de origen a auditar")):
    # Estructura de la query que definimos para limpiar el ruido
    query_body = {
        "size": 0,
        "query": {
            "bool": {
                "must": [
                    {
                        "term": {
                            "source.ip": ip
                        }
                    },
                    {
                        "regexp": {
                            "dns.question.name": {
                                "value": r".+\.(com|net|org|edu|gov|io|co|info|biz|live|office|windows|microsoft|whatsapp)(\..+)*",
                                "flags": "ALL",
                                "case_insensitive": True
                            }
                        }
                    }
                ],
                "must_not": [
                    {
                        "wildcard": {
                            "dns.question.name": "*.in-addr.arpa"
                        }
                    },
                    {
                        "wildcard": {
                            "dns.question.name": "*.local"
                        }
                    }
                ]
            }
        },
        "aggs": {
            "dominios_puros": {
                "terms": {
                    "field": "dns.question.name",
                    "size": 20
                }
            }
        }
    }

    try:
        # Ejecutamos la búsqueda en tus índices de red (ej: "filebeat-*")
        response = es.search(index="filebeat-*", body=query_body)
        
        # Extraemos únicamente los buckets resultantes (Filosofía de Endpoint limpio)
        buckets = response["aggregations"]["dominios_puros"]["buckets"]
        
        # Formateamos la respuesta final
        return {
            "success": True,
            "ip_auditada": ip,
            "data": [
                {"dominio": b["key"], "conexiones": b["doc_count"]} 
                for b in buckets
            ]
        }
        
    except Exception as e:
        return {
            "success": False,
            "error": f"Error al consultar Elasticsearch: {str(e)}"
        }

# =====================================================================
# US-005 : TOP DOMINIOS VISITADOS 
# =====================================================================

@app.get("/api/top-receptores")
def get_top_receptores(time_range: str = Query("now-1h", description="Rango de tiempo (ej. now-1h, now-24h)")):
    # Definimos el cuerpo de la consulta estructurado para Python
    query_body = {
        "size": 0,
        "query": {
            "bool": {
                "must": [
                    {
                        "range": {
                            "@timestamp": {
                                "gte": time_range,
                                "lte": "now"
                            }
                        }
                    },
                    {
                        "term": {
                            "destination.ip": "195.0.0.0/16"
                        }
                    }
                ]
            }
        },
        "aggs": {
            "top_receptores_internos": {
                "terms": {
                    "field": "destination.ip",
                    "size": 10,
                    "order": {
                        "bytes_recibidos": "desc"
                    }
                },
                "aggs": {
                    "bytes_recibidos": {
                        "sum": {
                            "field": "destination.bytes"
                        }
                    }
                }
            }
        }
    }

    try:
        # Ejecutamos la búsqueda en tus índices (compatible con Elasticsearch v8)
        response = es.search(index="filebeat-*", body=query_body)
        
        # Extraemos los buckets de la agregación
        buckets = response["aggregations"]["top_receptores_internos"]["buckets"]
        
        # Procesamos y formateamos los resultados
        resultado_limpio = []
        for b in buckets:
            bytes_puros = b["bytes_recibidos"]["value"]
            # Conversión matemática limpia a Megabytes (MB)
            megabytes = round(bytes_puros / (1024 * 1024), 2)
            
            resultado_limpio.append({
                "ip_destino": b["key"],
                "conexiones_totales": b["doc_count"],
                "bytes_recibidos": bytes_puros,
                "megabytes_recibidos": megabytes
            })
            
        return {
            "success": True,
            "rango_evaluado": time_range,
            "segmento_filtrado": "195.0.0.0/16",
            "data": resultado_limpio
        }
        
    except Exception as e:
        return {
            "success": False,
            "error": f"Error al consultar los receptores en Elasticsearch: {str(e)}"
        }

# =====================================================================
# EJECUCIÓN LOCAL
# =====================================================================
if __name__ == "__main__":
    import uvicorn
    # Corre el backend localmente en el puerto 8000
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)