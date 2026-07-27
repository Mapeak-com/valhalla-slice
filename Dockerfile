FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN pip install --no-cache-dir docopt fastapi uvicorn mercantile

COPY valhalla-slice /app/valhalla-slice

RUN chmod +x /app/valhalla-slice

RUN mkdir /valhalla

EXPOSE 8000

# Check the /list endpoint using standard library Python tools
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/list')" || exit 1

ENTRYPOINT ["/app/valhalla-slice"]

CMD ["--tar=/valhalla/valhalla_tiles.tar"]
