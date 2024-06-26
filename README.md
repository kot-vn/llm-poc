# README

## Init

```shell
docker compose build
docker compose up -d
```

```shell
pip install -r requirements.txt
```

```shell
python manage.py migrate
```

## Serve

```shell
docker compose up -d
```

```shell
python manage.py runserver 0.0.0.0:8000
```

## cUrl

create knowledge

```shell
curl --location 'http://localhost:8000/knowledge' \
--form 'openai_api_key="key"' \
--form 'file=@"example.txt"'
```

delete knowledge

```shell
curl --location --request DELETE 'http://localhost:8000/knowledge' \
--form 'url="gs://bucket_name/example.txt"'
```

chat

```shell
curl --location 'http://localhost:8000/knowledge/retrieve' \
--form 'openai_api_key="key"' \
--form 'question="what is your name?"' \
--form 'user_id="user_id"'
```
