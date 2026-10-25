// Door-counting logic (one FC-51 filter, two of them per door), free of Arduino calls so it can be unit
// tested on a PC.
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

enum Event : uint8_t { NONE = 0, PASSED = 1, BLOCKED = 2, CLEARED = 3, IN = 4, OUT = 5, ABORTED = 6 };

struct Config {
  uint16_t debounceMs;
  uint16_t minBlockMs;
  uint16_t maxBlockMs;
  uint16_t refractoryMs;
  uint16_t crossingGapMs;  // both beams clear this long = the crossing is over (see Doorway)
};

// Values tuned for the 1:20 model (a figure or a finger crossing a 5 cm doorway).
static const Config DEFAULT_CONFIG = {5, 60, 4000, 350, 400};

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

// Two FC-51 per door, one behind the other in the walking direction: OUTER (street side) and INNER (cabin
// side). The order in which the beams are interrupted gives the direction, so every door counts both ways
// and a passenger who turns back is reported instead of miscounted:
//   * a crossing starts when either beam is blocked and ends when both have been clear for `crossingGapMs`,
//     so the two legs of one person and the gap between the two beams belong to the same crossing;
//   * outer blocked first and inner blocked too = IN; inner first and outer too = OUT;
//   * only one beam blocked, or both at the very same instant = ABORTED: reported, never counted;
//   * an obstruction of either beam cancels the crossing (BLOCKED ... CLEARED), as with a single sensor.
class Doorway {
 public:
  explicit Doorway(const Config& config = DEFAULT_CONFIG)
      : config_(config),
        outer_(config),
        inner_(config),
        first_(NOBODY),
        sawOuter_(false),
        sawInner_(false),
        start_(0),
        lastBlocked_(0),
        lastCount_(0),
        hasCounted_(false),
        obstructed_(false) {}

  Event update(bool outerBlocked, bool innerBlocked, uint32_t nowMs) {
    const Event outerEvent = outer_.update(outerBlocked, nowMs);
    const Event innerEvent = inner_.update(innerBlocked, nowMs);
    if (!obstructed_ && (outerEvent == BLOCKED || innerEvent == BLOCKED)) {
      obstructed_ = true;
      first_ = NOBODY;
      return BLOCKED;
    }
    const bool outer = outer_.blocked();
    const bool inner = inner_.blocked();
    if (obstructed_) {
      if (outer || inner) {
        return NONE;
      }
      obstructed_ = false;
      return CLEARED;
    }
    if (first_ == NOBODY) {
      if (!outer && !inner) {
        return NONE;
      }
      first_ = outer && inner ? BOTH : (outer ? OUTER : INNER);
      sawOuter_ = outer;
      sawInner_ = inner;
      start_ = lastBlocked_ = nowMs;
      return NONE;
    }
    sawOuter_ = sawOuter_ || outer;
    sawInner_ = sawInner_ || inner;
    if (outer || inner) {
      lastBlocked_ = nowMs;
      return NONE;
    }
    if ((uint32_t)(nowMs - lastBlocked_) < config_.crossingGapMs) {
      return NONE;
    }
    return finish();
  }

  bool obstructed() const { return obstructed_; }

 private:
  enum First : uint8_t { NOBODY, OUTER, INNER, BOTH };

  Event finish() {
    const First first = first_;
    first_ = NOBODY;
    const uint32_t end = lastBlocked_;
    if ((uint32_t)(end - start_) < config_.minBlockMs) {
      return NONE;  // a glitch on one beam
    }
    if (first == BOTH || !(sawOuter_ && sawInner_)) {
      return ABORTED;
    }
    if (hasCounted_ && (uint32_t)(end - lastCount_) < config_.refractoryMs) {
      return NONE;
    }
    hasCounted_ = true;
    lastCount_ = end;
    return first == OUTER ? IN : OUT;
  }

  Config config_;
  Filter outer_;
  Filter inner_;
  First first_;
  bool sawOuter_;
  bool sawInner_;
  uint32_t start_;
  uint32_t lastBlocked_;
  uint32_t lastCount_;
  bool hasCounted_;
  bool obstructed_;
};

// Running totals of every door: boardings (IN) and alightings (OUT).
struct Totals {
  uint32_t boarded;
  uint32_t alighted;
  void reset() { boarded = 0; alighted = 0; }
  int32_t onBoard() const { return boarded >= alighted ? (int32_t)(boarded - alighted) : 0; }
};

}  // namespace door

#endif  // DOOR_LOGIC_H
