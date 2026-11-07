plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.kotlin.serialization)
}

android {
    namespace = "com.thomaswcode.phonerelay"
    compileSdk = 36

    defaultConfig {
        applicationId = "com.thomaswcode.phonerelay"
        // Android 14: the specialUse foreground service, and every accessibility API the app drives the
        // phone with (screenshots, the shade's dismissal, the d-pad actions), with no version checks.
        minSdk = 34
        targetSdk = 36
        versionCode = 5
        versionName = "1.4.0"
    }

    signingConfigs {
        // This app's own key, kept out of the public repository (git-ignored): it signs an app with an
        // accessibility service and the relay's device token, so a published key would let anyone build
        // an update that installs over it. Without the file (CI, a fresh clone) the default debug key is used.
        if (file("debug.keystore").exists()) {
            getByName("debug") {
                storeFile = file("debug.keystore")
                storePassword = "android"
                keyAlias = "androiddebugkey"
                keyPassword = "android"
            }
        }
    }

    buildTypes {
        debug {
            signingConfig = signingConfigs.getByName("debug")
        }
        release {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }

    testOptions {
        unitTests.isReturnDefaultValues = true
    }
}

dependencies {
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.activity.compose)
    implementation(libs.androidx.lifecycle.runtime.compose)
    implementation(platform(libs.androidx.compose.bom))
    implementation(libs.androidx.compose.ui)
    implementation(libs.androidx.compose.material3)
    implementation(libs.kotlinx.coroutines.android)
    implementation(libs.kotlinx.serialization.json)
    implementation(libs.okhttp)
    implementation(libs.libadb.android)
    implementation(libs.bouncycastle.pkix)
    implementation(libs.conscrypt.android)

    testImplementation(libs.junit)
    testImplementation(libs.kotlin.test.junit)
    testImplementation(libs.kotlinx.coroutines.test)
    testImplementation(libs.okhttp.mockwebserver)
}
