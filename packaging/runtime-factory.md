# Среда сборки Mac

prepare_macos_runtime.py готовит CPython 3.12.13 и зависимости из packaging/runtime-factory.lock.json. URL, версии, размеры и SHA-256 закреплены; модели и установленные пользовательские файлы не используются.

    python3 scripts/prepare_macos_runtime.py \
      --plan packaging/runtime-factory.lock.json \
      --output /private/tmp/meeting-runtime-source

Нужны Apple Silicon, macOS 14.4+, Python 3.12+ и инструменты командной строки Apple. Выходной каталог должен быть новым.

Результат: Python, зависимости и runtime-factory-receipt.json. Проверяются arm64, минимальная macOS, локальные библиотеки, лицензии и импорты без сети.

Для обновления зависимостей можно сохранить новый план через --plan-output NEW.json. Перед заменой lock-файла проверьте источники, лицензии и контрольные суммы.

Ручной workflow macos-package.yml собирает ZIP на macOS 26 ARM и проверяет сохранённый архив. Модели и звуковые устройства не запускаются.

[Упаковка приложения](README.md)
