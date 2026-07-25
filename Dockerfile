FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN pip install --no-cache-dir docopt fastapi uvicorn mercantile

COPY valhalla_slice /app/valhalla_slice

RUN chmod +x /app/valhalla_slice

RUN mkdir /valhalla

EXPOSE 8000

ENTRYPOINT ["/app/valhalla_slice"]

CMD ["--tar=/valhalla/valhalla_tiles.tar"]
