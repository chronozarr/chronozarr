// A synchronous backing tier for uint8 auxiliaries: peekMask must restore evicted
// decoded masks without an asynchronous codec or another network request. Uniform
// arrays cost two bytes; binary masks cost one bit/pixel. Other uint8 values remain
// exact (coverage counts and masks from external writers need not be binary).
export function packAux(data) {
  let uniform = true;
  let binary = true;
  for (let i = 0; i < data.length; i++) {
    if (data[i] !== data[0]) uniform = false;
    if (data[i] > 1) binary = false;
  }
  if (uniform) return Uint8Array.of(0, data[0] ?? 0);
  if (!binary) {
    const packed = new Uint8Array(data.length + 1);
    packed[0] = 2;
    packed.set(data, 1);
    return packed;
  }
  const packed = new Uint8Array(1 + Math.ceil(data.length / 8));
  packed[0] = 1;
  for (let i = 0; i < data.length; i++) packed[1 + (i >> 3)] |= data[i] << (i & 7);
  return packed;
}

export function unpackAux(packed, length) {
  if (packed[0] === 0) return new Uint8Array(length).fill(packed[1]);
  if (packed[0] === 2) return packed.slice(1);
  const data = new Uint8Array(length);
  for (let i = 0; i < length; i++) data[i] = (packed[1 + (i >> 3)] >> (i & 7)) & 1;
  return data;
}
