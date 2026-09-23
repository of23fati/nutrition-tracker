# 🍎 Трекер питания

Веб-приложение для учёта еды, воды и веса.

## Возможности

- Поиск продуктов (Open Food Facts + локальная база)
- Дневник по дням (вчера / сегодня / завтра / любой день)
- Приёмы пищи: завтрак, обед, ужин, перекус
- Вода (норма от веса)
- Вес и расчёт целей КБЖУ
- Избранное, импорт/экспорт CSV

## Локальный запуск

```bash
pip install -r requirements.txt
streamlit run nutrition_tracker_app.py
```

С телефона в одной Wi‑Fi:

```bash
streamlit run nutrition_tracker_app.py --server.address 0.0.0.0
```

Открой на телефоне: `http://IP-компьютера:8501`

## Деплой на Streamlit Cloud (свой URL)

1. Зарегистрируйся на [GitHub](https://github.com) и [share.streamlit.io](https://share.streamlit.io)
2. Создай репозиторий, загрузи туда:
   - `nutrition_tracker_app.py`
   - `requirements.txt`
3. На Streamlit Cloud: **New app** → выбери репозиторий → Main file: `nutrition_tracker_app.py`
4. Deploy → получишь ссылку вида `https://xxx.streamlit.app`

Каждый человек делает **свой** репозиторий и **своё** приложение — данные не пересекаются.

## Важно

Данные хранятся в файле `nutrition.db` рядом со скриптом.  
На Streamlit Cloud база живёт на сервере приложения (при передеплое может сброситься — делай экспорт CSV).
