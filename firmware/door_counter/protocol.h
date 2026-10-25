// Serial protocol between the Arduino and the on-board computer (plain ASCII lines, 115200 baud).
//
//   $BOOT,<firmware>,<ms>*CS                          after every reset of the Arduino
//   $DOOR,<seq>,<F|R>,<IN|OUT|ABORTED|BLOCKED|CLEARED>,<boarded>,<alighted>,<ms>*CS
//        both doors report IN and OUT; ABORTED = a crossing that turned back (not counted)
//   $HB,<seq>,<boarded>,<alighted>,<front_blocked>,<rear_blocked>,<noise_edges>,<ms>*CS   every second
//   $ACK,<RESET|STATUS|BUTTON>,<seq>*CS
//
// Every line carries the *cumulative* totals, so the Raspberry never depends on receiving every line:
// a lost line is detected by the sequence number and corrected by the next one. CS is the XOR of every
// character between '$' and '*', in two hexadecimal digits (the NMEA convention), so a corrupted line is
// discarded instead of producing a wrong count.
//
// Commands from the Raspberry (one per line): RESET (terminal auto-correction, RNF-6.3), STATUS.
#ifndef PROTOCOL_H
#define PROTOCOL_H

#include <stddef.h>
#include <stdint.h>

namespace protocol {

inline uint8_t checksum(const char* body) {
  uint8_t sum = 0;
  for (const char* c = body; *c != '\0'; ++c) {
    sum ^= (uint8_t)*c;
  }
  return sum;
}

// Writes "$<body>*CS\r\n" into `out`. Returns the length, or 0 when it does not fit.
inline size_t frame(const char* body, char* out, size_t capacity) {
  static const char HEX_DIGITS[] = "0123456789ABCDEF";
  size_t length = 0;
  while (body[length] != '\0') {
    ++length;
  }
  const size_t total = length + 6;  // '$' + body + '*' + 2 hex + "\r\n"
  if (total + 1 > capacity) {
    return 0;
  }
  const uint8_t sum = checksum(body);
  out[0] = '$';
  for (size_t i = 0; i < length; ++i) {
    out[i + 1] = body[i];
  }
  out[length + 1] = '*';
  out[length + 2] = HEX_DIGITS[(sum >> 4) & 0x0F];
  out[length + 3] = HEX_DIGITS[sum & 0x0F];
  out[length + 4] = '\r';
  out[length + 5] = '\n';
  out[length + 6] = '\0';
  return total;
}

// Accumulates command characters; returns true when a full line is ready in `line()`.
class CommandReader {
 public:
  CommandReader() : length_(0), overflow_(false) { buffer_[0] = '\0'; }

  bool feed(char c) {
    if (c == '\r') {
      return false;
    }
    if (c == '\n') {
      const bool ready = !overflow_ && length_ > 0;
      buffer_[length_] = '\0';
      length_ = 0;
      overflow_ = false;
      return ready;
    }
    if (length_ + 1 >= sizeof(buffer_)) {
      overflow_ = true;  // a line that is too long is dropped whole
      return false;
    }
    buffer_[length_++] = c;
    return false;
  }

  const char* line() const { return buffer_; }

 private:
  char buffer_[24];
  size_t length_;
  bool overflow_;
};

inline bool equals(const char* a, const char* b) {
  while (*a != '\0' && *a == *b) {
    ++a;
    ++b;
  }
  return *a == *b;
}

}  // namespace protocol

#endif  // PROTOCOL_H
