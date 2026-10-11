Задача **{{ key }}: {{ title }}**{% if day %} (день {{ day }} плана){% endif %}.

{{ description }}

Критерии готовности:
{% for criterion in criteria %}
- {{ criterion }}
{% endfor %}
{% if depends_on %}
Опирается на выполненные задачи: {{ depends_on | join(", ") }}.
{% endif %}

Ветка: `{{ branch }}` (уже создана и выбрана). Команда тестов: `{{ test_command }}`.
{% if restarted %}

Задача уже начиналась в другой сессии: посмотри коммиты в ветке и `docs/JOURNAL.md` и продолжай с того места.
{% endif %}
