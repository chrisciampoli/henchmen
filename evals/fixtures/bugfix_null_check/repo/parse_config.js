// Fixture module with a null-reference bug for the bugfix_null_check eval.
// Do not fix this file outside the harness.

function parseConfig(input) {
  // BUG: does not null-check ``input`` before property access. Passing
  // ``undefined`` or ``null`` crashes with a TypeError. The fix is to
  // return ``{}`` when input is nullish.
  const key = input.key;
  return {
    key: key,
    hasKey: Boolean(key),
  };
}

module.exports = { parseConfig };
