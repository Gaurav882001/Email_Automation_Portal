# Image Gen
## Installation

### Prerequisites

#### 1. Install Python
Install ```python-3.10.12``` and ```python-pip```. Follow the steps from the below reference document based on your Operating System.
Reference: [https://docs.python-guide.org/starting/installation/](https://docs.python-guide.org/starting/installation/)

#### 2. Install PostgresSql
Install ```PostgreSQL 14.18```. Follow the steps form the below reference document based on your Operating System.
Reference: [https://www.postgresql.org/](https://www.postgresql.org/)
#### 3. Setup virtual environment
```bash
# Install virtual environment
python -m venv venv

# Activate virtual environment
source venv/bin/activate
```

#### 4. Clone git repository
```bash
git clone 
```

#### 5. Install requirements
```bash
cd image_gen/
pip install -r requirements.txt
```

#### 6. Edit project settings
```bash
# Edit Database configurations with your MySQL configurations.
# Search for DATABASES section.
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': '',
        'USER': '',
        'PASSWORD': '',
        'HOST': '',
        'PORT': '',
    }
}


# Edit email configurations.
# Search for email configurations
EMAIL_SENDER=
GMAIL_APP_PASSWORD=

```
#### 7. Run the server
```bash
# Make migrations
python manage.py makemigrations
python manage.py migrate

# For search feature we need to index certain tables to the haystack. For that run below command.
python manage.py runserver

# Run the server
python manage.py runserver 0:8000

# your server is up on port 8000
```
Try opening [http://localhost:8000](http://localhost:8000) in the browser.
Now you are good to go.
