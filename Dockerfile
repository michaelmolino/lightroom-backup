FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir pipenv

# Copy Pipfile and Pipfile.lock first for caching
COPY Pipfile Pipfile.lock ./
RUN pipenv install --system --deploy

# Copy the rest of the source code
COPY src/ .

CMD ["python", "app.py"]
