
# Étape 1 : construire le frontend React/Vite
FROM node:22-alpine AS frontend-build

WORKDIR /app

COPY package*.json ./
RUN npm ci

COPY App.jsx main.jsx index.html styles.css vite.config.js ./
RUN npm run build

# Étape 2 : lancer le backend Flask
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY gtfs_rt_prouhon.py web_app.py ./
COPY --from=frontend-build /app/dist ./frontend/dist

ENV PORT=10000
EXPOSE 10000

CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT:-10000} --workers 1 --threads 8 web_app:app"]


