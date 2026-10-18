// Host-side tests of the firmware logic (no Arduino needed):
//   g++ -std=c++11 -Wall -Wextra -Werror -I ../door_counter test_firmware_logic.cpp -o test && ./test
#include <cstdio>
#include <cstring>

#include "door_logic.h"
#include "protocol.h"

static int failures = 0;
#define CHECK(condition)                                                   \
  do {                                                                     \
    if (!(condition)) {                                                    \
      std::printf("FAIL line %d: %s\n", __LINE__, #condition);             \
      ++failures;                                                          \
    }                                                                      \
  } while (0)

// Holds `blocked` from `from` to `to` (1 ms samples) and returns the events seen.
static int run(door::Filter& filter, bool blocked, unsigned from, unsigned to, door::Event* last) {
  int events = 0;
  for (unsigned t = from; t < to; ++t) {
    door::Event event = filter.update(blocked, t);
    if (event != door::NONE) {
      ++events;
      *last = event;
    }
  }
  return events;
}

static void a_normal_passage_counts_once() {
  door::Filter filter;
  door::Event last = door::NONE;
  CHECK(run(filter, false, 0, 100, &last) == 0);
  CHECK(run(filter, true, 100, 400, &last) == 0);  // 300 ms in front of the sensor
  CHECK(run(filter, false, 400, 500, &last) == 1);
  CHECK(last == door::PASSED);
}

static void a_glitch_is_ignored() {
  door::Filter filter;
  door::Event last = door::NONE;
  run(filter, false, 0, 50, &last);
  CHECK(run(filter, true, 50, 53, &last) == 0);  // 3 ms < debounce
  CHECK(run(filter, false, 53, 200, &last) == 0);
  CHECK(run(filter, true, 200, 230, &last) == 0);  // 30 ms < minimum block
  CHECK(run(filter, false, 230, 400, &last) == 0);
}

static void two_legs_of_one_person_count_once() {
  door::Filter filter;
  door::Event last = door::NONE;
  run(filter, false, 0, 10, &last);
  run(filter, true, 10, 200, &last);
  CHECK(run(filter, false, 200, 300, &last) == 1);  // first leg
  run(filter, true, 300, 400, &last);
  CHECK(run(filter, false, 400, 450, &last) == 0);  // second leg, 200 ms later: refractory
  run(filter, true, 900, 1100, &last);
  CHECK(run(filter, false, 1100, 1200, &last) == 1);  // next person
}

static void an_obstruction_is_reported_not_counted() {
  door::Filter filter;
  door::Event last = door::NONE;
  run(filter, false, 0, 10, &last);
  CHECK(run(filter, true, 10, 5000, &last) == 1);
  CHECK(last == door::BLOCKED);
  CHECK(filter.obstructed());
  CHECK(run(filter, false, 5000, 5100, &last) == 1);
  CHECK(last == door::CLEARED);
  CHECK(!filter.obstructed());
}

static void millis_overflow_is_harmless() {
  door::Filter filter;
  door::Event last = door::NONE;
  const unsigned start = 0xFFFFFF00u;
  int events = 0;
  for (unsigned i = 0; i < 100; ++i) events += filter.update(false, start + i) != door::NONE;
  for (unsigned i = 100; i < 400; ++i) {  // crosses 0xFFFFFFFF -> 0
    door::Event e = filter.update(true, start + i);
    if (e != door::NONE) { ++events; last = e; }
  }
  for (unsigned i = 400; i < 500; ++i) {
    door::Event e = filter.update(false, start + i);
    if (e != door::NONE) { ++events; last = e; }
  }
  CHECK(events == 1 && last == door::PASSED);
}

static void totals_never_go_negative() {
  door::Totals totals = {2, 5};
  CHECK(totals.onBoard() == 0);
  totals.boarded = 9;
  CHECK(totals.onBoard() == 4);
  totals.reset();
  CHECK(totals.boarded == 0 && totals.alighted == 0);
}

static void frames_carry_an_nmea_checksum() {
  char out[64];
  size_t n = protocol::frame("HB,1,2,3", out, sizeof(out));
  CHECK(n == std::strlen(out));
  unsigned sum = 0;
  for (const char* c = "HB,1,2,3"; *c; ++c) sum ^= (unsigned char)*c;
  char expected[32];
  std::snprintf(expected, sizeof(expected), "$HB,1,2,3*%02X\r\n", sum);
  CHECK(std::strcmp(out, expected) == 0);
  char tiny[8];
  CHECK(protocol::frame("TOO-LONG-BODY", tiny, sizeof(tiny)) == 0);
}

static void commands_are_read_line_by_line() {
  protocol::CommandReader reader;
  const char* input = "RES";
  for (const char* c = input; *c; ++c) CHECK(!reader.feed(*c));
  CHECK(!reader.feed('E'));
  CHECK(!reader.feed('T'));
  CHECK(!reader.feed('\r'));
  CHECK(reader.feed('\n'));
  CHECK(protocol::equals(reader.line(), "RESET"));
  for (int i = 0; i < 40; ++i) reader.feed('X');  // too long: dropped
  CHECK(!reader.feed('\n'));
  CHECK(!reader.feed('\n'));  // empty line
  const char* status = "STATUS\n";
  bool ready = false;
  for (const char* c = status; *c; ++c) ready = reader.feed(*c);
  CHECK(ready && protocol::equals(reader.line(), "STATUS"));
  CHECK(!protocol::equals("STAT", "STATUS"));
}

int main() {
  a_normal_passage_counts_once();
  a_glitch_is_ignored();
  two_legs_of_one_person_count_once();
  an_obstruction_is_reported_not_counted();
  millis_overflow_is_harmless();
  totals_never_go_negative();
  frames_carry_an_nmea_checksum();
  commands_are_read_line_by_line();
  if (failures == 0) {
    std::printf("firmware logic: all tests passed\n");
    return 0;
  }
  std::printf("firmware logic: %d failure(s)\n", failures);
  return 1;
}
