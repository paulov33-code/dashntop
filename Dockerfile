# Usamos una imagen ligera de Python basada en Debian
FROM python:3.12-slim

# Evita que Python escriba archivos .pyc y fuerza el buffering de logs para verlos en tiempo real
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Establece el directorio de trabajo dentro del contenedor
WORKDIR /app

# Instala dependencias del sistema si tu código las necesita (ej: git, gcc, etc.)
# RUN apt-get update && apt-get install -y --no-install-recommends gcc && rm -rf /var/lib/apt/lists/*

# Copia e instala los requerimientos de Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia el resto del código de tu aplicación
COPY . .

# Comando por defecto para arrancar tu app (cambia 'main.py' por tu archivo principal)
CMD ["python", "main.py"]