import os

filepath = r'c:\Users\a\Desktop\local-openshelf\app\files\templates\files\base.html'
with open(filepath, 'r', encoding='utf-8') as f:
    text = f.read()

target = "즐겨찾기\n        </a>"
replacement = target + """

        <a href="{% url 'files:page_ai_history' %}"
           class="{% if request.resolver_match.url_name == 'page_ai_history' %}active{% endif %}">
          <svg viewBox="0 0 24 24"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path></svg>
          AI 검색 기록
        </a>"""

if target in text:
    text = text.replace(target, replacement)
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(text)
    print("Success")
else:
    print("Target not found")
