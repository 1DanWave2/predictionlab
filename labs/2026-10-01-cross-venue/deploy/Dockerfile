# Cross-venue price collector (Lab 3). Build context = the lab directory.
FROM python:3.12-slim
RUN pip install --no-cache-dir "httpx>=0.27" "pandas>=2.2" "pyarrow>=16" tzdata
WORKDIR /app
COPY teams.py fetch.py match.py collect.py ./
ENV XVENUE_DATA_DIR=/data PYTHONUNBUFFERED=1
CMD ["python3", "collect.py", "--db", "/data/xvenue.db", "--interval", "300", "--rematch-hours", "6"]
