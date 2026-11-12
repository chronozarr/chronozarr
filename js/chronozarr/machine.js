// A lifecycle written as data: the states an object can be in, and which event leads from which state to which.
//
// The table maps every state to an object of `event: target state`; a state with no moves is `{}`. The constructor
// checks the table, so a typo in it fails when the module that owns it loads. `send` takes a move the current state
// lists and throws on any other, leaving the state as it was. The object that owns a machine does what the move
// requires right after `send` returns. Code reads the state by comparing `state`, which allocates nothing on a path
// that runs per frame or per chunk.

const isPlainObject = (value) => {
  if (value === null || typeof value !== 'object') return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
};

export class Machine {
  #name;
  #table;
  #state;

  constructor(name, table, initial) {
    if (typeof name !== 'string' || name === '') {
      throw new TypeError(`Machine: the name must be a non-empty string, got ${String(name)}; name the object whose lifecycle this is.`);
    }
    if (!isPlainObject(table)) {
      throw new TypeError(`${name}: the table must be a plain object that maps each state to its moves, got ${String(table)}.`);
    }
    for (const [state, stateMoves] of Object.entries(table)) {
      if (!isPlainObject(stateMoves)) {
        throw new TypeError(`${name}: the moves of state "${state}" must be a plain object of event: target state (write {} for none), got ${String(stateMoves)}.`);
      }
      for (const [event, target] of Object.entries(stateMoves)) {
        if (typeof target !== 'string' || !Object.hasOwn(table, target)) {
          throw new Error(`${name}: state "${state}" moves on "${event}" to "${String(target)}", which is not a state of the table; add the state or fix the target.`);
        }
      }
    }
    if (typeof initial !== 'string' || !Object.hasOwn(table, initial)) {
      throw new Error(`${name}: the initial state "${String(initial)}" is not a state of the table (states: ${Object.keys(table).join(', ')}).`);
    }
    this.#name = name;
    this.#table = table;
    this.#state = initial;
  }

  get state() {
    return this.#state;
  }

  /** Whether the current state accepts `event`. */
  can(event) {
    return Object.hasOwn(this.#table[this.#state], event);
  }

  /** Takes the move `event` names from the current state and returns the new state; throws if there is none. */
  send(event) {
    const stateMoves = this.#table[this.#state];
    if (!Object.hasOwn(stateMoves, event)) {
      const accepted = Object.keys(stateMoves);
      const accepts = accepted.length === 0 ? 'it accepts nothing' : `it accepts: ${accepted.join(', ')}`;
      throw new Error(`${this.#name}: "${String(event)}" is not a move from "${this.#state}" (${accepts})`);
    }
    this.#state = stateMoves[event];
    return this.#state;
  }
}
