FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY gtfs_rt_prouhon.py irigo_map_refactored.py web_app.py index.html ./

ENV PORT=10000
EXPOSE 10000
CMD ["sh", "-c", "python irigo_map_refactored.py & exec gunicorn --bind 0.0.0.0:${PORT:-10000} --workers 1 --threads 8 web_app:app"]
