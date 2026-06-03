from fastapi import APIRouter, HTTPException, Query
from typing import Optional
import httpx
import asyncio

router = APIRouter(
    prefix="/hosts",
    tags=["Hosts Telemetry"]
)

# Configuración de conexión con tu instancia de ntopng
NTOPNG_BASE_URL = "http://195.0.4.100:3000" 
NTOPNG_USERNAME = "admin"
NTOPNG_PASSWORD = "Passw0rd.29384756"

def clasificar_categoria(proto_name: str) -> str:
    """
    Helper para agrupar los protocolos/servicios de ntopng 
    en las macro-categorías del gráfico de torta (Web o Apps).
    """
    proto_lower = proto_name.lower()
    
    # Servicios identificados netamente como Aplicaciones
    apps_keywords = ['spotify', 'netflix', 'slack', 'discord', 'zoom', 'teams', 'whatsapp', 'telegram']
    if any(kw in proto_lower for kw in apps_keywords):
        return "Apps"
        
    # Protocolos base de navegación o dominios web genéricos
    web_keywords = ['http', 'ssl', 'tls', 'facebook', 'google', 'youtube', 'infobae', 'instagram', 'twitter', 'tiktok']
    if any(kw in proto_lower for kw in web_keywords):
        return "Web"
        
    return "Otros"

@router.get("/{ip}")
async def get_host_detail(ip: str, ifid: Optional[int] = Query(4)):
    """
    Obtiene telemetría en tiempo real desde la API de ntopng para una IP específica,
    extrayendo y estructurando dinámicamente el campo 'l7_categories'.
    """
    
    async with httpx.AsyncClient(auth=(NTOPNG_USERNAME, NTOPNG_PASSWORD), verify=False, follow_redirects=True) as client:
        try:
            # Endpoints con credenciales embebidas para máxima compatibilidad con scripts .lua internos
            url_details = f"{NTOPNG_BASE_URL}/lua/rest/get/host/data.lua?ifid={ifid}&host={ip}&user={NTOPNG_USERNAME}&password={NTOPNG_PASSWORD}"
            url_l7 = f"{NTOPNG_BASE_URL}/lua/rest/get/host/l7/vlan/data.lua?ifid={ifid}&host={ip}&user={NTOPNG_USERNAME}&password={NTOPNG_PASSWORD}"
            
            # Ejecución en paralelo
            res_details, res_l7 = await asyncio.gather(
                client.get(url_details, timeout=5.0),
                client.get(url_l7, timeout=5.0)
            )
            
            print(f"\n[DEBUG NOC] IP Solicitada: {ip}")
            print(f"[DEBUG NOC] -> URL Final Details: {res_details.url}")
            print(f"[DEBUG NOC] -> Status Final Details: {res_details.status_code}")
            print(f"[DEBUG NOC] -> Status Final L7: {res_l7.status_code}")
            
            # 1. CONTROL DE SEGURIDAD: Verificar si nos botó al login
            if "login.lua" in str(res_details.url):
                print("[ERROR CRÍTICO] ntopng rechazó las credenciales y redirigió al Login.")
                raise HTTPException(status_code=401, detail="Credenciales incorrectas de ntopng en backend")
            
            # 2. CONTROL DE FLUJO MOCK: Si ntopng redirige a página 'not_found' o responde 404
            if res_details.status_code == 404 or "message=not_found" in str(res_details.url):
                print(f"[WARM NOC] Host {ip} no encontrado en tiempo real (ifid={ifid}). Retornando estructura vacía segura.")
                return {
                    "ip": ip,
                    "hostname": f"Host-{ip.split('.')[-1]}",
                    "mac": "00:00:00:00:00:00",
                    "vendor": "Desconocido (Inactivo)",
                    "os": "Desconocido",
                    "is_online": False,
                    "bytes_sent": 0,
                    "packets_sent": 0,
                    "bytes_rcvd": 0,
                    "packets_rcvd": 0,
                    "active_flows": 0,
                    "l7_categories": [
                        {"category": "Web", "bytes": 0, "items": []},
                        {"category": "Apps", "bytes": 0, "items": []},
                        {"category": "Otros", "bytes": 0, "items": []}
                    ]
                }
            
            # 3. CONTROL DE ERRORES GENÉRICOS (500, 503, etc.)
            if res_details.status_code != 200 or res_l7.status_code != 200:
                raise HTTPException(
                    status_code=502, 
                    detail=f"ntopng respondió con error. Status: Details={res_details.status_code}, L7={res_l7.status_code}"
                )
                
            ntop_data = res_details.json()
            ntop_l7 = res_l7.json()
            
        except httpx.RequestError as exc:
            print(f"\n[ERROR CRÍTICO NOC] Fallo de conexión: {exc}")
            raise HTTPException(status_code=503, detail=f"Error de comunicación: {exc}")

    # Procesar información base del Host si la consulta fue exitosa (200 OK)
    host_info = ntop_data.get("rsp", ntop_data) if isinstance(ntop_data, dict) else {}
    
    # Inicializar el mapeador para el gráfico de torta y drill-down
    categorias_map = {
        "Web": {"category": "Web", "bytes": 0, "items": []},
        "Apps": {"category": "Apps", "bytes": 0, "items": []},
        "Otros": {"category": "Otros", "bytes": 0, "items": []}
    }
    
    # Procesar filas de Capa 7 devueltas por ntopng
    l7_rows = ntop_l7.get("rsp", {}).get("rows", []) if isinstance(ntop_l7, dict) else []
    
    for row in l7_rows:
        proto_name = row.get("name", "Desconocido")
        proto_bytes = int(row.get("bytes", 0))
        
        if proto_bytes == 0:
            continue
            
        target_cat = clasificar_categoria(proto_name)
        categorias_map[target_cat]["bytes"] += proto_bytes
        categorias_map[target_cat]["items"].append({
            "name": proto_name.lower(),
            "bytes": proto_bytes
        })

    # Estructurar, ordenar y filtrar categorías
    l7_categories_final = []
    for cat_name, cat_data in categorias_map.items():
        if cat_data["bytes"] > 0:
            # Ordenamos de mayor a menor y limitamos al Top 5 interno para limpieza visual en React
            cat_data["items"] = sorted(cat_data["items"], key=lambda x: x["bytes"], reverse=True)[:5]
            l7_categories_final.append(cat_data)

    return {
        "ip": host_info.get("ip", ip),
        "hostname": host_info.get("name", host_info.get("asn_name", f"Host-{ip}")),
        "mac": host_info.get("mac", "00:00:00:00:00:00"),
        "vendor": host_info.get("vendor", "Generic/Unknown"),
        "os": host_info.get("os", "Desconocido"),
        "is_online": host_info.get("seen_last", 0) > 0 or host_info.get("active_flows", 0) > 0,
        "bytes_sent": int(host_info.get("bytes_sent", 0)),
        "packets_sent": int(host_info.get("packets_sent", 0)),
        "bytes_rcvd": int(host_info.get("bytes_rcvd", 0)),
        "packets_rcvd": int(host_info.get("packets_rcvd", 0)),
        "active_flows": int(host_info.get("active_flows", 0)),
        "l7_categories": l7_categories_final
    }