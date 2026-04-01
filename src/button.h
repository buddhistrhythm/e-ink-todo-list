#ifndef __BUTTON_H__
#define __BUTTON_H__

#include <OneButton.h>
#include "network.h"
#include "gpio16.h"
#include "store.h"
#include "aiUsage.h"

OneButton button;

void onWakeUp()
{
  Serial.println("onWakeUp");
}

void goToSleep(u32_t time)
{
  Serial.println("goToSleep");
  wifi_set_opmode(NULL_MODE);
  wifi_fpm_set_sleep_type(LIGHT_SLEEP_T);
  wifi_fpm_open();
  // gpio_pin_wakeup_enable(16, GPIO_PIN_INTR_LOLEVEL);
  wifi_fpm_set_wakeup_cb(onWakeUp);
  wifi_fpm_do_sleep(time);
}

void buttonClick()
{
  Serial.println("Button clicked");
  if (wifiManager.getConfigPortalActive())
  {
    Serial.println("Close Config Portal");
    wifiManager.stopConfigPortal();
    return;
  }

  if (WiFi.status() == WL_CONNECTED)
  {
    updating = true;
    if (currentDisplayMode == MODE_AI_USAGE)
    {
      Serial.println("force update AI usage");
      memset(aiUsageLastModified, 0, sizeof(aiUsageLastModified));
      downloadAndDrawAiUsage();
    }
    else
    {
      Serial.println("force update todo");
      cleanRunningValue();
      downloadAndDrawTodo();
    }
    updating = false;
  }
}

void buttonDoubleClick()
{
  Serial.println("Button double clicked");
  switchDisplayMode();

  if (currentDisplayMode == MODE_AI_USAGE)
  {
    Serial.println("Switching to AI Usage display");
    if (WiFi.status() == WL_CONNECTED)
    {
      updating = true;
      downloadAndDrawAiUsage();
      updating = false;
    }
    else
    {
      showCachedAiUsage();
    }
  }
  else
  {
    Serial.println("Switching to Todo display");
    if (WiFi.status() == WL_CONNECTED)
    {
      cleanRunningValue();
      updating = true;
      downloadAndDrawTodo();
      updating = false;
    }
  }
}

void buttonLongPress()
{
  Serial.println("Button long pressed");
  Serial.println("Erasing configuration、persistent value and restarting...");
  wifiManager.resetSettings();
  removePersistentValue();
  ESP.restart();
}

// Handler function for MultiClick the button with self pointer as a parameter
void buttonMultiClick()
{
  Serial.print("Button multi clicked: ");
  Serial.println(button.getNumberClicks());

  switch (button.getNumberClicks())
  {
  case 4:
    ESP.restart();
    break;
  case 5:
    removePersistentValue();
    break;
  case 6:
    if (WiFi.status() == WL_CONNECTED)
    {
      Serial.println("Disconnecting WiFi");
      wifiManager.disconnect();
    }
  default:
    break;
  }
}

OneButton initButton()
{

  Serial.println("Initializing button...");

  Serial.print("Button pin: ");
  Serial.println(BUTTON_PIN);

  if (BUTTON_PIN == 16)
  {
    Serial.println("Button pin is 16, configuring gpio16_output_conf...");
    gpio16_output_conf();
    gpio16_output_set(1);
    button = OneButton(BUTTON_PIN);
  }
  else
  {
    button = OneButton(BUTTON_PIN);
  }

  button.setPressMs(10000);

  button.attachClick(buttonClick);
  button.attachDoubleClick(buttonDoubleClick);
  button.attachLongPressStart(buttonLongPress);

  // MultiClick button event attachment with self pointer as a parameter
  button.attachMultiClick(buttonMultiClick);

  return button;
}

void buttonLoop()
{
  button.tick();
}

#endif