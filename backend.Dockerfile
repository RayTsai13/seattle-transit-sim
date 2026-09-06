FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV HEATMAP_GEOJSON=/app/seattle/data/processed/seattle_heatmap_grid.geojson

COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend/*.py ./backend/
COPY seattle/data/processed/seattle_heatmap_grid.geojson ./seattle/data/processed/seattle_heatmap_grid.geojson

EXPOSE 8000

CMD ["uvicorn", "backend.server:app", "--host", "0.0.0.0", "--port", "8000"]
