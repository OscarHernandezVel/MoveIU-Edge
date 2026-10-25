// Door counter for the 1:20 bus model: Arduino UNO R3 + four FC-51 infrared obstacle sensors, two per door.
//
// Wiring (see edge/docs/wiring.svg):
//   Front door  FC-51 OUTER (street side) OUT -> D2    FC-51 INNER (cabin side) OUT -> D5
//   Rear door   FC-51 OUTER (street side) OUT -> D3    FC-51 INNER (cabin side) OUT -> D6
//   Every FC-51: VCC -> 5V, GND -> GND. The two sensors of a door sit 15-25 mm apart in the walking direction.
//   Green LED  (boarding)   D8  -> 220 ohm -> LED -> GND
//   Red LED    (alighting)  D9  -> 220 ohm -> LED -> GND
//   Yellow LED (obstructed) D10 -> 220 ohm -> LED -> GND
//   Push button (terminal reset, optional) D4 -> button -> GND  (internal pull-up)
//   USB cable to the on-board computer: power and serial data (115200 baud).
//
// Outer then inner = boarding, inner then outer = alighting, at either door (door_logic.h, Doorway). A crossing
// that blocks only one beam is reported as ABORTED and not counted. The Arduino only counts; it never decides
// occupancy. It reports cumulative totals, so the computer recovers from lost lines (see protocol.h). No
// camera, image or personal data ever touches this board.

#include "door_logic.h"
#include "protocol.h"

static const char FIRMWARE[] = "door-counter-2.0";

static const uint8_t PIN_FRONT_OUTER = 2;
static const uint8_t PIN_REAR_OUTER = 3;
static const uint8_t PIN_FRONT_INNER = 5;
static const uint8_t PIN_REAR_INNER = 6;
static const uint8_t PIN_BUTTON = 4;
static const uint8_t PIN_LED_IN = 8;
static const uint8_t PIN_LED_OUT = 9;
static const uint8_t PIN_LED_BLOCKED = 10;
static const uint8_t PIN_LED_ALIVE = LED_BUILTIN;
static const uint8_t SENSOR_PINS[4] = {PIN_FRONT_OUTER, PIN_FRONT_INNER, PIN_REAR_OUTER, PIN_REAR_INNER};

static const uint32_t HEARTBEAT_MS = 1000;
static const uint32_t FLASH_MS = 150;
static const uint32_t BUTTON_DEBOUNCE_MS = 50;

static door::Doorway frontDoor;
static door::Doorway rearDoor;
static door::Totals totals = {0, 0};
static protocol::CommandReader commands;

static uint16_t sequence = 0;
static uint32_t lastHeartbeat = 0;
static uint32_t ledInUntil = 0;
static uint32_t ledOutUntil = 0;
static bool buttonWasDown = false;
static uint32_t buttonChangedAt = 0;

// Raw level changes of the four sensors: a diagnostic for noisy wiring or sunlight (reported in the heartbeat).
// Polled, not by interrupt: only D2 and D3 have interrupts on the UNO.
static uint16_t noiseEdges = 0;
static bool lastRaw[4] = {false, false, false, false};

// FC-51: LOW = obstacle in front of the sensor. Also counts raw edges.
static bool sensorBlocked(uint8_t index) {
  const bool blocked = digitalRead(SENSOR_PINS[index]) == LOW;
  if (blocked != lastRaw[index]) {
    lastRaw[index] = blocked;
    noiseEdges++;
  }
  return blocked;
}

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
  char body[72];
  snprintf(body, sizeof(body), "HB,%u,%lu,%lu,%d,%d,%u,%lu", sequence++, (unsigned long)totals.boarded,
           (unsigned long)totals.alighted, frontDoor.obstructed() ? 1 : 0, rearDoor.obstructed() ? 1 : 0,
           noiseEdges, (unsigned long)now);
  noiseEdges = 0;
  send(body);
}

static void sendAck(const char* what) {
  char body[32];
  snprintf(body, sizeof(body), "ACK,%s,%u", what, sequence++);
  send(body);
}

static void handleDoor(door::Event event, char door, uint32_t now) {
  switch (event) {
    case door::IN:
      totals.boarded++;
      ledInUntil = now + FLASH_MS;
      sendDoor(door, "IN", now);
      break;
    case door::OUT:
      totals.alighted++;
      ledOutUntil = now + FLASH_MS;
      sendDoor(door, "OUT", now);
      break;
    case door::ABORTED:
      sendDoor(door, "ABORTED", now);
      break;
    case door::BLOCKED:
      sendDoor(door, "BLOCKED", now);
      break;
    case door::CLEARED:
      sendDoor(door, "CLEARED", now);
      break;
    default:
      break;
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
  for (uint8_t i = 0; i < 4; ++i) {
    pinMode(SENSOR_PINS[i], INPUT);  // the FC-51 drives its output (push-pull); no pull-up needed
  }
  pinMode(PIN_BUTTON, INPUT_PULLUP);
  pinMode(PIN_LED_IN, OUTPUT);
  pinMode(PIN_LED_OUT, OUTPUT);
  pinMode(PIN_LED_BLOCKED, OUTPUT);
  pinMode(PIN_LED_ALIVE, OUTPUT);
  Serial.begin(115200);
  char body[40];
  snprintf(body, sizeof(body), "BOOT,%s,%lu", FIRMWARE, (unsigned long)millis());
  send(body);
}

void loop() {
  const uint32_t now = millis();
  const bool frontOuter = sensorBlocked(0);
  const bool frontInner = sensorBlocked(1);
  const bool rearOuter = sensorBlocked(2);
  const bool rearInner = sensorBlocked(3);
  handleDoor(frontDoor.update(frontOuter, frontInner, now), 'F', now);
  handleDoor(rearDoor.update(rearOuter, rearInner, now), 'R', now);
  handleCommands(now);
  handleButton(now);

  if ((uint32_t)(now - lastHeartbeat) >= HEARTBEAT_MS) {
    lastHeartbeat = now;
    sendHeartbeat(now);
    digitalWrite(PIN_LED_ALIVE, !digitalRead(PIN_LED_ALIVE));
  }
  digitalWrite(PIN_LED_IN, (int32_t)(ledInUntil - now) > 0 ? HIGH : LOW);
  digitalWrite(PIN_LED_OUT, (int32_t)(ledOutUntil - now) > 0 ? HIGH : LOW);
  digitalWrite(PIN_LED_BLOCKED, frontDoor.obstructed() || rearDoor.obstructed() ? HIGH : LOW);
}
