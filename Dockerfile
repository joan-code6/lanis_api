FROM python:3.11-slim

WORKDIR /app

# Install dependencies first so this layer is cached unless requirements change
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the project
COPY . .

ENV LANIS_API_HOST=0.0.0.0
ENV LANIS_API_PORT=8000

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "api.api:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
