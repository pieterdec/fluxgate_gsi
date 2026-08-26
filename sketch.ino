#include <Arduino_RouterBridge.h>
#include <ADS1115-SOLDERED.h>

// Declare your ADC object
ADS1115 ADS;

// How long to wait between readings. This used to be a hardcoded delay(1000),
// which meant changing the sample rate required editing this file and
// reflashing the board. It's now a variable that the Python side can change
// at runtime over the Bridge (see set_sample_interval / get_sample_interval
// below and the /sample_rate endpoint in main.py) - no reflash needed.
volatile unsigned long sample_interval_ms = 1000;

// Called from Python via Bridge.call("set_sample_interval", ms)
bool set_sample_interval(int ms)
{
    if (ms < 50) ms = 50;              // safety floor: don't hammer the ADC/bridge
    if (ms > 3600000) ms = 3600000;    // safety ceiling: at most once per hour
    sample_interval_ms = (unsigned long)ms;
    return true;
}

// Called from Python via Bridge.call("get_sample_interval")
int get_sample_interval()
{
    return (int)sample_interval_ms;
}

// --- PGA gain -------------------------------------------------------------
// Valid values for this ADS1115 library are 0, 1, 2, 4, 8, 16, mapping to
// full-scale ranges of +-6.144V, +-4.096V, +-2.048V, +-1.024V, +-0.512V,
// +-0.256V respectively - smaller range = finer LSB = better resolution,
// as long as the signal doesn't exceed that range (which would clip it).
//
// IMPORTANT: setGain() writes the ADS1115's config register and restarts
// its conversion cycle. Calling it on every loop() pass (as this used to
// do) was the root cause of an earlier bug where readings were biased by
// the sample rate - fast loops kept interrupting conversions before they
// could settle. Gain is now only touched here, on demand, when Python
// asks for a change - never inside loop().
volatile uint8_t current_gain = 0;

bool set_gain(int gain)
{
    if (gain != 0 && gain != 1 && gain != 2 && gain != 4 && gain != 8 && gain != 16) {
        return false; // not one of the six valid PGA settings
    }
    ADS.setGain(gain);
    current_gain = (uint8_t)gain;
    return true;
}

// Called from Python via Bridge.call("get_gain")
int get_gain()
{
    return (int)current_gain;
}

void setup()
{
    // Initialize the Serial communication
    Serial.begin(115200);
    Serial.println(__FILE__);
    Serial.print("ADS1X15_LIB_VERSION: ");
    Serial.println(ADS1X15_LIB_VERSION);

    // Initialize the ADC
    ADS.begin();

    // Gain and data rate are configured once here, not on every loop() -
    // see the long comment above set_gain(). Data rate 1 = a slow
    // conversion rate, which averages over a longer window internally and
    // rejects noise (including mains hum) much better than the fastest
    // setting - worth it since these long measurement runs only need
    // ~1 sample/second anyway. If your ADS1115-SOLDERED library uses
    // different setDataRate() enum values than expected, check its header
    // for the right constant.
    ADS.setGain(0);
    current_gain = 0;
    ADS.setDataRate(1);

    Bridge.begin();
    Bridge.provide("set_sample_interval", set_sample_interval);
    Bridge.provide("get_sample_interval", get_sample_interval);
    Bridge.provide("set_gain", set_gain);
    Bridge.provide("get_gain", get_gain);
}

void loop()
{
    // Read the values from each analog input channel.
    // readADC() triggers a fresh single-shot conversion and blocks until it
    // completes, so - with gain/data-rate fixed in setup() and only ever
    // changed on demand via set_gain() - this always returns a settled
    // reading at whatever gain is currently configured.
    int16_t val_0 = ADS.readADC(0);

    // Calculate voltage from raw ADC values. toVoltage() reflects whatever
    // gain is currently active, so this stays correct automatically as
    // gain changes (manually or via auto-gain).
    float f = ADS.toVoltage(1); // Voltage factor
    float voltage = val_0 * f;  // adc.computeVolts(val_0);

    Serial.print("\tAnalog0: ");
    Serial.print(val_0);
    Serial.print('\t');
    Serial.print(voltage);

    Bridge.notify("record_sensor_samples", voltage);

    delay(sample_interval_ms);
}
