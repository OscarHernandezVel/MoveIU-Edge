// Door counter for the 1:20 bus model: Arduino UNO R3 + two FC-51 infrared obstacle sensors.
//
// Wiring (see edge/docs/wiring.svg):
//   FC-51 front door  OUT -> D2 (INT0)   VCC -> 5V   GND -> GND     counts boardings  (+1)
//   FC-51 rear door   OUT -> D3 (INT1)   VCC -> 5V   GND -> GND     counts alightings (-1)
//   Green LED  (boarding)   D8  -> 220 ohm -> LED -> GND
//   Red LED    (alighting)  D9  -> 220 ohm -> LED -> GND
//   Yellow LED (obstructed) D10 -> 220 ohm -> LED -> GND
//   Push button (terminal reset, optional) D4 -> button -> GND  (internal pull-up)
//   USB cable to the Raspberry Pi: power and serial data (115200 baud).
//
// The Arduino only counts; it never decides occupancy. It reports cumulative totals, so the Raspberry
// recovers from lost lines (see protocol.h). No camera, image or personal data ever touches this board.

#include "door_logic.h"
#include "protocol.h"

static const char FIRMWARE[] = "door-counter-1.0";

static const uint8_t PIN_FRONT = 2;
static const uint8_t PIN_REAR = 3;
static const uint8_t PIN_BUTTON = 4;
static const uint8_t PIN_LED_IN = 8;
static const uint8_t PIN_LED_OUT = 9;
static const uint8_t PIN_LED_BLOCKED = 10;
static const uint8_t PIN_LED_ALIVE = LED_BUILTIN;

static const uint32_t HEARTBEAT_MS = 1000;
static const uint32_t FLASH_MS = 150;
static const uint32_t BUTTON_DEBOUNCE_MS = 50;

static door::Filter frontFilter;
static door::Filter rearFilter;
static door::Totals totals = {0, 0};
static protocol::CommandReader commands;

static uint16_t sequence = 0;
static uint32_t lastHeartbeat = 0;
static uint32_t ledInUntil = 0;
static uint32_t ledOutUntil = 0;
static bool buttonWasDown = false;
static uint32_t buttonChangedAt = 0;

// Raw edges seen by the interrupts: a diagnostic for noisy wiring or sunlight (reported in the heartbeat).
static volatile uint16_t noiseEdges = 0;
static void onEdge() { noiseEdges++; }

static void send(const char* body) {
  char line[80];
  if (protocol::frame(body, line, sizeof(line)) > 0) {
    Serial.print(line);
  }
}

static void sendDoor(char door, const char* event, uint32_t now) {
  char body[64];
  snprintf(body, sizeof(body), "DOOR,%u,%c,%s,%lu,%lu,%lu", sequence++, door, event,
           (unsigned long)totals.boarded, (unsigned long)totals.alighted, (unsigned long)now);
  send(body);
}

static void sendHeartbeat(uint32_t now) {
  uint16_t edges;
  noInterrupts();
  edges = noiseEdges;
  noiseEdges = 0;
  interrupts();
  char body[72];
  snprintf(body, sizeof(body), "HB,%u,%lu,%lu,%d,%d,%u,%lu", sequence++, (unsigned long)totals.boarded,
           (unsigned long)totals.alighted, frontFilter.obstructed() ? 1 : 0, rearFilter.obstructed() ? 1 : 0,
           edges, (unsigned long)now);
  send(body);
}

static void sendAck(const char* what) {
  char body[32];
  snprintf(body, sizeof(body), "ACK,%s,%u", what, sequence++);
  send(body);
}

static void handleDoor(door::Event event, char door, bool boarding, uint32_t now) {
  if (event == door::PASSED) {
    if (boarding) {
      totals.boarded++;
      ledInUntil = now + FLASH_MS;
    } else {
      totals.alighted++;
      ledOutUntil = now + FLASH_MS;
    }
    sendDoor(door, boarding ? "IN" : "OUT", now);
  } else if (event == door::BLOCKED) {
    sendDoor(door, "BLOCKED", now);
  } else if (event == door::CLEARED) {
    sendDoor(door, "CLEARED", now);
  }
}

static void handleCommands(uint32_t now) {
  while (Serial.available() > 0) {
    if (!commands.feed((char)Serial.read())) {
      continue;
    }
    if (protocol::equals(commands.line(), "RESET")) {
      totals.reset();
      sendAck("RESET");
    } else if (protocol::equals(commands.line(), "STATUS")) {
      sendAck("STATUS");
      sendHeartbeat(now);
    }
  }
}

static void handleButton(uint32_t now) {
  const bool down = digitalRead(PIN_BUTTON) == LOW;
  if (down != buttonWasDown && (uint32_t)(now - buttonChangedAt) >= BUTTON_DEBOUNCE_MS) {
    buttonWasDown = down;
    buttonChangedAt = now;
    if (down) {
      totals.reset();
      sendAck("BUTTON");
    }
  }
}

void setup() {
  pinMode(PIN_FRONT, INPUT);  // the FC-51 drives its output (push-pull); no pull-up needed
  pinMode(PIN_REAR, INPUT);
  pinMode(PIN_BUTTON, INPUT_PULLUP);
  pinMode(PIN_LED_IN, OUTPUT);
  pinMode(PIN_LED_OUT, OUTPUT);
  pinMode(PIN_LED_BLOCKED, OUTPUT);
  pinMode(PIN_LED_ALIVE, OUTPUT);
  attachInterrupt(digitalPinToInterrupt(PIN_FRONT), onEdge, CHANGE);
  attachInterrupt(digitalPinToInterrupt(PIN_REAR), onEdge, CHANGE);
  Serial.begin(115200);
  char body[40];
  snprintf(body, sizeof(body), "BOOT,%s,%lu", FIRMWARE, (unsigned long)millis());
  send(body);
}

void loop() {
  const uint32_t now = millis();
  // FC-51: LOW = obstacle in front of the sensor.
  handleDoor(frontFilter.update(digitalRead(PIN_FRONT) == LOW, now), 'F', true, now);
  handleDoor(rearFilter.update(digitalRead(PIN_REAR) == LOW, now), 'R', false, now);
  handleCommands(now);
  handleButton(now);

  if ((uint32_t)(now - lastHeartbeat) >= HEARTBEAT_MS) {
    lastHeartbeat = now;
    sendHeartbeat(now);
    digitalWrite(PIN_LED_ALIVE, !digitalRead(PIN_LED_ALIVE));
  }
  digitalWrite(PIN_LED_IN, (int32_t)(ledInUntil - now) > 0 ? HIGH : LOW);
  digitalWrite(PIN_LED_OUT, (int32_t)(ledOutUntil - now) > 0 ? HIGH : LOW);
  digitalWrite(PIN_LED_BLOCKED, frontFilter.obstructed() || rearFilter.obstructed() ? HIGH : LOW);
}
