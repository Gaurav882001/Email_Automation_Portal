#!/bin/bash
set -e
 
echo "Waiting for PostgreSQL to be ready..."
echo "DB HOST IS $DB_HOST"
echo "DB PORT IS $DB_PORT"
echo "DB USER IS $DB_USER"
echo "DB PASSWORD IS $DB_PASSWORD"
 
# Wait until PostgreSQL responds
until PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -U "$DB_USER" -p "$DB_PORT" -d "$DB_NAME" -c '\q'; do
    echo "Waiting for PostgreSQL at $DB_HOST:$DB_PORT with user $DB_USER and db $DB_NAME..."
    sleep 2
done
 
echo "PostgreSQL is up - running migrations..."
python manage.py makemigrations --noinput
python manage.py migrate --noinput

  
echo "Starting Django server..."
python manage.py runserver 0.0.0.0:8001