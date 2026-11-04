# Сборка для Apple Silicon

Нужны macOS 14.4+, Apple Silicon, Python 3.12+ и инструменты командной строки Apple.

    python3 scripts/prepare_macos_runtime.py \
      --plan packaging/runtime-factory.lock.json \
      --output /private/tmp/meeting-runtime-source

    python3 scripts/package_macos.py \
      --runtime-source /private/tmp/meeting-runtime-source \
      --worker-source "$PWD/src" \
      --version 0.7.0 --build 18 \
      --output .build.noindex/MeetingSummary-0.7.0-beta18-macos-arm64.zip

Каталог runtime-source должен отсутствовать перед запуском.

ZIP включает приложение, Python и зависимости. Модели скачиваются при первой подготовке: около 6,6 ГБ для 9B или 3,7 ГБ для экспериментального 4B. Для подготовки нужно около 13 ГБ свободного места.

Сборщик проверяет зависимости, локальную подпись, содержимое архива и импорты. Рядом сохраняется JSON-отчёт. Модели и аудиоустройства эти проверки не запускают.

Готовые Mac-сборки имеют ad-hoc подпись и SHA-256. macOS может потребовать ручного разрешения первого запуска.

[Среда сборки](runtime-factory.md) · [Тесты](../docs/TESTING.md) · [Лицензии](../THIRD_PARTY_NOTICES.md)
