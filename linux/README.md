# Сводка встречи для Linux

Ubuntu 24.04 / Debian 12–13, x86_64 с AVX2, PulseAudio или PipeWire-Pulse. Рекомендуется 16 ГБ памяти и минимум 10 ГБ свободного места. GPU не требуется.

## Установка

Скачайте пакет из [Releases](https://github.com/Markiz-Ksan/meeting-summary/releases).

DEB:

    sudo apt install ./MeetingSummary-*-linux-amd64.deb

AppImage:

    chmod +x MeetingSummary-*-linux-x86_64.AppImage
    ./MeetingSummary-*-linux-x86_64.AppImage

Python, Qt и движки включены. При первом нажатии «Начать» скачиваются модели — около 6,8 ГБ. Затем обработка работает локально.

Источник звука выбирается в окне или меню трея. WAV и TXT сохраняются с общим именем в «Документы/Сводка встреч/YYYY-MM-DD».

## Ограничения

Бета-версия: модели и длинные встречи в сборке 18 не тестировались. На CPU возможна задержка; Wayland может ограничивать положение окна.

Если AppImage не запускается без FUSE:

    APPIMAGE_EXTRACT_AND_RUN=1 ./MeetingSummary-*-linux-x86_64.AppImage

## Разработка

Зависимости сборки перечислены в .github/workflows/linux-build.yml и linux/requirements-build.txt.

    bash linux/build.sh

Пакеты появляются в .build.noindex/linux. [Тесты](../docs/TESTING.md) · [Лицензии](THIRD_PARTY.md)
