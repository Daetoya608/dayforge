{% if secret_name %}
Пользователь добавил секрет `{{ secret_name }}` в `.env`. Продолжай задачу.
{% else %}
Ответы пользователя:

{% for item in answers %}
- **{{ item.question }}** — {{ item.answer }}
{% endfor %}

Продолжай задачу с учётом ответов.
{% endif %}
