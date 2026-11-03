# ZXing 二维码
-keep class com.google.zxing.** { *; }
-dontwarn com.google.zxing.**

# WebView 相关
-keepclassmembers class * {
    @android.webkit.JavascriptInterface <methods>;
}
