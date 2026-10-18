// Door-counting logic for one infrared FC-51 sensor, free of Arduino calls so it can be unit tested on a PC.
//
// The FC-51 output is LOW while something reflects the beam (a passenger, a finger, a figure in the model).
// A raw signal is noisy: sunlight flicker, a hand that hesitates, a bag that stays in the doorway.
// The filter turns it into clean events (RNF-6.1: filter false positives):
//
//   1. Debounce: the level must stay stable for `debounceMs` before it is accepted.
//   2. A passage is a block followed by a release, lasting at least `minBlockMs` (shorter = noise).
//   3. A block longer than `maxBlockMs` is an obstruction (bag, umbrella, someone standing in the door):
//      it is reported once as BLOCKED and its release as CLEARED, and it is NOT counted.
//   4. After a counted passage, releases within `refractoryMs` are ignored (the two legs of one person).
//
// All time arithmetic uses unsigned subtraction, so it survives the millis() overflow every 49.7 days.
#ifndef DOOR_LOGIC_H
#define DOOR_LOGIC_H

#include <stdint.h>

namespace door {

enum Event : uint8_t { NONE = 0, PASSED = 1, BLOCKED = 2, CLEARED = 3 };

struct Config {
  uint16_t debounceMs;
  uint16_t minBlockMs;
  uint16_t maxBlockMs;
  uint16_t refractoryMs;
};

// Values tuned for the 1:20 model (a figure or a finger crossing a 5 cm doorway).
static const Config DEFAULT_CONFIG = {5, 60, 4000, 350};

class Filter {
 public:
  explicit Filter(const Config& config = DEFAULT_CONFIG)
      : config_(config),
        candidate_(false),
        candidateSince_(0),
        stable_(false),
        blockStart_(0),
        lastPass_(0),
        hasPassed_(false),
        obstructed_(false) {}

  // Feed one sample: `blocked` is true while the beam is interrupted. Returns at most one event.
  Event update(bool blocked, uint32_t nowMs) {
    if (blocked != candidate_) {
      candidate_ = blocked;
      candidateSince_ = nowMs;
    }
    if (stable_ == candidate_) {
      if (stable_ && !obstructed_ && (uint32_t)(nowMs - blockStart_) > config_.maxBlockMs) {
        obstructed_ = true;
        return BLOCKED;
      }
      return NONE;
    }
    if ((uint32_t)(nowMs - candidateSince_) < config_.debounceMs) {
      return NONE;
    }
    stable_ = candidate_;
    const uint32_t edge = candidateSince_;
    if (stable_) {
      blockStart_ = edge;
      return NONE;
    }
    // Released.
    if (obstructed_) {
      obstructed_ = false;
      return CLEARED;
    }
    const uint32_t duration = edge - blockStart_;
    if (duration < config_.minBlockMs) {
      return NONE;
    }
    if (hasPassed_ && (uint32_t)(edge - lastPass_) < config_.refractoryMs) {
      return NONE;
    }
    hasPassed_ = true;
    lastPass_ = edge;
    return PASSED;
  }

  bool blocked() const { return stable_; }
  bool obstructed() const { return obstructed_; }

 private:
  Config config_;
  bool candidate_;
  uint32_t candidateSince_;
  bool stable_;
  uint32_t blockStart_;
  uint32_t lastPass_;
  bool hasPassed_;
  bool obstructed_;
};

// Running totals. With one sensor per door (the model's choice) the front door counts boardings and the
// rear door counts alightings, as in the design document ("+1 pasajero" / "-1 pasajero").
struct Totals {
  uint32_t boarded;
  uint32_t alighted;
  void reset() { boarded = 0; alighted = 0; }
  int32_t onBoard() const { return boarded >= alighted ? (int32_t)(boarded - alighted) : 0; }
};

}  // namespace door

#endif  // DOOR_LOGIC_H
