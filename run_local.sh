#!/bin/bash
set -e

# Script to run Django server locally without Docker
# Make sure PostgreSQL is running and accessible

echo "Activating virtual environment..."
source venv/bin/activate

echo "Loading environment variables..."
if [ -f .env ]; then
    export $(cat .env | grep -v '^#' | xargs)
else
    echo "Warning: .env file not found. Make sure environment variables are set."
fi

echo "Checking PostgreSQL connection..."
until PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -U "$DB_USER" -p "${DB_PORT:-5432}" -d "$DB_NAME" -c '\q' 2>/dev/null; do
    echo "Waiting for PostgreSQL at $DB_HOST:${DB_PORT:-5432}..."
    echo "Make sure PostgreSQL is running. You can start it with: docker-compose up db -d"
    sleep 2
done

echo "PostgreSQL is up - running migrations..."
python manage.py makemigrations --noinput
python manage.py migrate --noinput

echo "Starting Django server on http://localhost:8001"
echo "Press Ctrl+C to stop"
echo ""
# Use -u flag to disable Python output buffering so print statements show immediately
python -u manage.py runserver 0.0.0.0:8001

