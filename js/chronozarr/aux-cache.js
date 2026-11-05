// A synchronous backing tier for uint8 auxiliaries: peekMask must restore evicted
// decoded masks without an asynchronous codec or another network request. Uniform
// arrays cost two bytes; binary masks cost one bit/pixel. Other uint8 values remain
// exact (coverage counts and masks from external writers need not be binary).
export function packAux(data) {
  const n = data.length;
  // The scan reads four bytes at a time, which needs a view that starts on a 4-byte boundary (a subarray may not).
  // A uniform array is one whose every word is its first byte repeated four times; a binary one has no bit set above
  // the lowest of any byte. The trailing bytes that do not fill a word are checked one by one.
  const bytes = data.byteOffset % 4 === 0 ? data : data.slice();
  const words = new Uint32Array(bytes.buffer, bytes.byteOffset, n >> 2);
  const fill = (data[0] ?? 0) * 0x01010101;
  let differ = 0;
  let high = 0;
  for (let i = 0; i < words.length; i++) {
    differ |= words[i] ^ fill;
    high |= words[i];
  }
  for (let i = words.length * 4; i < n; i++) {
    differ |= data[i] ^ data[0];
    high |= data[i];
  }
  if (differ === 0) return Uint8Array.of(0, data[0] ?? 0);
  if ((high & 0xfefefefe) !== 0) {
    const packed = new Uint8Array(n + 1);
    packed[0] = 2;
    packed.set(data, 1);
    return packed;
  }
  const packed = new Uint8Array(1 + Math.ceil(n / 8));
  packed[0] = 1;
  const whole = n >> 3;
  for (let j = 0, i = 0; j < whole; j++, i += 8) {
    packed[1 + j] = data[i] | (data[i + 1] << 1) | (data[i + 2] << 2) | (data[i + 3] << 3) | (data[i + 4] << 4) | (data[i + 5] << 5) | (data[i + 6] << 6) | (data[i + 7] << 7);
  }
  for (let i = whole * 8; i < n; i++) packed[1 + (i >> 3)] |= data[i] << (i & 7);
  return packed;
}

export function unpackAux(packed, length) {
  if (packed[0] === 0) return new Uint8Array(length).fill(packed[1]);
  if (packed[0] === 2) return packed.slice(1);
  const data = new Uint8Array(length);
  for (let i = 0; i < length; i++) data[i] = (packed[1 + (i >> 3)] >> (i & 7)) & 1;
  return data;
}
