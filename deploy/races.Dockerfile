FROM python:3.13-slim
WORKDIR /app
RUN pip install --no-cache-dir httpx==0.28.1 beautifulsoup4==4.15.0
COPY src/health_agent/__init__.py /app/health_agent/__init__.py
COPY src/health_agent/races/__init__.py src/health_agent/races/sources.py src/health_agent/races/schedule.py src/health_agent/races/cloud.py /app/health_agent/races/
COPY races-2027.json race-windows-2027.json /app/
ENV PYTHONUNBUFFERED=1 PORT=8080 RACE_STATE_DIR=/data
CMD ["python", "-m", "health_agent.races.cloud"]
