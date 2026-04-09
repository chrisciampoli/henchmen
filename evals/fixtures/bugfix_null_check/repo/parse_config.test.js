const { parseConfig } = require("./parse_config");

test("parseConfig returns empty object when input is undefined", () => {
  expect(parseConfig(undefined)).toEqual({});
});

test("parseConfig returns empty object when input is null", () => {
  expect(parseConfig(null)).toEqual({});
});

test("parseConfig still returns key when provided", () => {
  expect(parseConfig({ key: "value" })).toEqual({ key: "value", hasKey: true });
});
