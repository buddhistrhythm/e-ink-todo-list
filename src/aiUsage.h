#ifndef AI_USAGE_H
#define AI_USAGE_H

#include <algorithm>
#include <GxEPD2_BW.h>
#include "display.h"
#include "store.h"
#include "config.h"
#include "clock.h"
#include "network.h"
#include <FS.h>
#include <LittleFS.h>
#include <ESP8266HTTPClient.h>
#include <CertStoreBearSSL.h>
#include <ArduinoJson.h>

// Display mode management
enum DisplayMode
{
  MODE_TODO = 0,
  MODE_AI_USAGE = 1,
};

DisplayMode currentDisplayMode = MODE_TODO;
String aiUsageCachedFile = "/ai_usage.bitmap";
char aiUsageLastModified[30] = "";

void switchDisplayMode()
{
  if (currentDisplayMode == MODE_TODO)
  {
    currentDisplayMode = MODE_AI_USAGE;
    Serial.println("Switched to AI Usage mode");
  }
  else
  {
    currentDisplayMode = MODE_TODO;
    Serial.println("Switched to Todo mode");
  }
}

/**
 * Download AI usage dashboard bitmap from the server.
 * The server renders usage stats (from Claude/Cursor/Codex) as a bitmap
 * that fits the e-ink display dimensions.
 *
 * API endpoint: {apiUrl}/ai-usage?width=W&height=H
 * Response: binary bitmap (same format as todo bitmap)
 */
void downloadAndDrawAiUsage()
{
  if (wifiManager.getConfigPortalActive())
  {
    Serial.println("Config portal active, skip AI usage update");
    return;
  }

  const char *apiRoot = apiUrl.getValue();
  const char *apikey = apiKey.getValue();

  BearSSL::CertStore certStore;
  BearSSL::WiFiClientSecure client;

  if (!LittleFS.begin())
  {
    Serial.println("An Error has occurred while mounting LittleFS");
    return;
  }

  int numCerts = certStore.initCertStore(LittleFS, PSTR("/certs.idx"), PSTR("/certs.ar"));
  Serial.printf("Number of CA certs read: %d\n", numCerts);
  if (numCerts == 0)
  {
    Serial.println("No certs found, using insecure connection");
    LittleFS.end();
    client.setInsecure();
  }
  else
  {
    client.setCertStore(&certStore);
  }

  // Build URL: append /ai-usage to the base API URL
  String baseUrl = String(apiRoot);
  // Remove trailing path segments to get the base, then append /ai-usage
  // e.g., https://www.einktodo.com/api/display/v2 -> https://www.einktodo.com/api/display/v2/ai-usage
  String url = baseUrl + "/ai-usage?width=" + String(display.width()) + "&height=" + String(display.height());

  Serial.printf("GET AI Usage: %s\n", url.c_str());
  Serial.printf("If-Modified-Since: %s\n", aiUsageLastModified);

  HTTPClient https;
  https.begin(client, url);
  https.addHeader("If-Modified-Since", String(aiUsageLastModified));
  https.addHeader("Authorization", "Bearer " + String(apikey));
  https.addHeader("X-Device-Id", DeviceID);
  https.addHeader("X-Display-Mode", "ai-usage");
#ifdef GIT_VERSION
  https.addHeader("X-Device-Firmware-Version", GIT_VERSION);
#endif

  const char *headerKeys[] = {"Content-Picture-Width", "Content-Picture-Height", "Last-Modified"};
  int headerKeysSize = sizeof(headerKeys) / sizeof(char *);
  https.collectHeaders(headerKeys, headerKeysSize);

  int httpCode = https.GET();
  int contentLength = https.getSize();

  Serial.printf("AI Usage HTTPS GET: %d\n", httpCode);

  if (httpCode == HTTP_CODE_NOT_MODIFIED)
  {
    Serial.println("AI Usage: Not Modified");
    https.end();
    LittleFS.end();
    // Show cached version if available
    if (LittleFS.begin())
    {
      if (LittleFS.exists(aiUsageCachedFile))
      {
        LittleFS.end();
        // Re-display cached file if mode just switched
        return;
      }
      LittleFS.end();
    }
    return;
  }

  if (httpCode == HTTP_CODE_NO_CONTENT || httpCode == 404)
  {
    Serial.println("AI Usage: No data available");
    https.end();
    LittleFS.end();
    showTextOnScreenCenter("AI Usage\nNo data yet\n\nRun collector:\npython tools/ai-usage-collector.py", 1);
    return;
  }

  if (httpCode == 401)
  {
    Serial.println("AI Usage: Auth failed");
    https.end();
    LittleFS.end();
    showTextOnScreenCenter("AI Usage\nAPI Key auth failed!");
    return;
  }

  if (httpCode != HTTP_CODE_OK)
  {
    Serial.printf("AI Usage HTTPS GET failed: %s\n", https.errorToString(httpCode).c_str());
    https.end();
    LittleFS.end();
    return;
  }

  if (contentLength <= 0)
  {
    Serial.println("AI Usage: Content-Length not set");
    https.end();
    LittleFS.end();
    return;
  }

  WiFiClient *stream = https.getStreamPtr();
  uint16_t w = https.header("Content-Picture-Width").toInt();
  uint16_t h = https.header("Content-Picture-Height").toInt();
  String lastModified = https.header("Last-Modified");

  // Save bitmap to flash
  LittleFS.begin();
  File file = LittleFS.open(aiUsageCachedFile, "w");
  if (!file)
  {
    Serial.printf("Cannot open %s for writing\n", aiUsageCachedFile.c_str());
    https.end();
    LittleFS.end();
    return;
  }

  Serial.println("Downloading AI usage bitmap...");
  uint8_t buff[128];
  size_t buffSize = sizeof(buff);
  size_t readSize;
  size_t readSizeTotal = 0;
  while (https.connected() && (readSize = stream->readBytes(buff, std::min(buffSize, (size_t)(contentLength - readSizeTotal)))) > 0)
  {
    file.write(buff, readSize);
    readSizeTotal += readSize;
    Serial.printf("\rAI Usage download: %d%% (%d/%d)", (readSizeTotal * 100) / contentLength, readSizeTotal, contentLength);
  }
  Serial.println();
  file.close();
  LittleFS.end();

  https.end();

  strcpy(aiUsageLastModified, lastModified.c_str());
  delay(50);
  displayToScreen(aiUsageCachedFile, w, h, GxEPD_BLACK);
}

/**
 * Show the cached AI usage bitmap if available.
 * Used when switching display modes without re-downloading.
 */
void showCachedAiUsage()
{
  if (!LittleFS.begin())
  {
    return;
  }

  if (LittleFS.exists(aiUsageCachedFile))
  {
    LittleFS.end();
    // Read cached dimensions from persistent storage
    uint16_t w = getPersistentValue("ai_usage_w", (uint16_t)display.width());
    uint16_t h = getPersistentValue("ai_usage_h", (uint16_t)display.height());
    displayToScreen(aiUsageCachedFile, w, h, GxEPD_BLACK);
  }
  else
  {
    LittleFS.end();
    showTextOnScreenCenter("AI Usage\nNo cached data\nWaiting for update...");
  }
}

#endif // AI_USAGE_H
