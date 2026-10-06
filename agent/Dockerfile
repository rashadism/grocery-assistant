FROM python:3.12-slim
# Without this, stdout is block-buffered in a container (not a TTY) - our
# own flush=True prints still work, but Strands' internal tool-progress
# prints don't pass it, so kubectl logs showed nothing at all for an
# entire live turn until the buffer happened to fill.
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py agent.py tools.py ./
RUN useradd --uid 10001 --create-home appuser
USER appuser
EXPOSE 8080
CMD ["python", "app.py"]
