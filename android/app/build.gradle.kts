plugins {
    id("com.android.application")
    id("com.chaquo.python")
}

android {
    namespace = "it.gpupool.app"
    compileSdk = 34
    defaultConfig {
        applicationId = "it.gpupool.app"
        minSdk = 29
        targetSdk = 34
        versionCode = (project.findProperty("vc") as String? ?: "1").toInt()
        versionName = "1.0.$versionCode"
        ndk { abiFilters += listOf("arm64-v8a", "armeabi-v7a") }
    }
    signingConfigs {
        getByName("debug") {      // chiave di sviluppo fissa: gli aggiornamenti si installano sopra la versione precedente
            storeFile = file("../gpool-debug.keystore")
            storePassword = "android"
            keyAlias = "androiddebugkey"
            keyPassword = "android"
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    lint { abortOnError = false }
}

chaquopy {
    defaultConfig { version = "3.11" }
}
