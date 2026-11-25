# Android-оболонка для приймання

Оболонка відкриває окремий тестовий HTTPS-сервіс. Підтримує вибір кількох фото з галереї й захоплення вихідного JPEG камерою. Файли тестового лота передаються прямо застосунку.

Потрібні JDK 17, Gradle 8.13, Android SDK Platform 36 та Build Tools 36.0.0. Цільовий API 36 відповідає чинній вимозі Google Play для нових публікацій. [Вимоги Google Play](https://support.google.com/googleplay/android-developer/answer/11926878)

```bash
gradle -p android -PvintageOrigin=https://vintage-ua-ebay-p094-test.onrender.com \
  :app:lintDebug :app:assembleDebug :app:bundleRelease
```

GitHub Actions зберігає APK для тестового встановлення й AAB. Debug APK підписаний тестовим ключем CI. Release AAB готується для окремого підпису власником ключем Google Play. Публікація в Play Console потребує приймання й окремого дозволу. Ключі підпису зберігаються поза Git.
