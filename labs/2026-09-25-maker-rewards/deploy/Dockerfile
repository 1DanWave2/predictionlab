FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir httpx==0.28.1 pandas==2.2.3 numpy==2.2.6
COPY shadow_maker.py report.py /app/
CMD ["python3", "-u", "shadow_maker.py", "--db", "/data/shadow.db", "--interval", "60"]
