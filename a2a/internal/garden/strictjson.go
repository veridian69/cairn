package garden

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"reflect"
	"strings"
)

// checkJSON rejects duplicate keys and case aliases before encoding/json can
// silently collapse them. Map keys remain data, while struct keys are exact.
func checkJSON(data []byte, shape reflect.Type) error {
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.UseNumber()
	if err := checkValue(dec, shape, 0); err != nil {
		return err
	}
	if _, err := dec.Token(); err != io.EOF {
		return errors.New("trailing JSON")
	}
	return nil
}
func checkValue(dec *json.Decoder, shape reflect.Type, depth int) error {
	if depth > 24 {
		return errors.New("JSON nesting too deep")
	}
	token, err := dec.Token()
	if err != nil {
		return err
	}
	switch token {
	case json.Delim('{'):
		if shape.Kind() != reflect.Struct && shape.Kind() != reflect.Map {
			return errors.New("unexpected JSON object")
		}
		fields := map[string]reflect.Type{}
		if shape.Kind() == reflect.Struct {
			for i := 0; i < shape.NumField(); i++ {
				f := shape.Field(i)
				name := strings.Split(f.Tag.Get("json"), ",")[0]
				fields[name] = f.Type
			}
		}
		seen := map[string]bool{}
		for dec.More() {
			key, err := dec.Token()
			if err != nil {
				return err
			}
			name, ok := key.(string)
			if !ok || seen[name] {
				return errors.New("duplicate JSON key")
			}
			seen[name] = true
			var child reflect.Type
			if shape.Kind() == reflect.Map {
				child = shape.Elem()
			} else {
				child = fields[name]
				if child == nil {
					return errors.New("unknown JSON field")
				}
			}
			if err = checkValue(dec, child, depth+1); err != nil {
				return err
			}
		}
		end, err := dec.Token()
		if err != nil || end != json.Delim('}') {
			return errors.New("invalid JSON object")
		}
	case json.Delim('['):
		if shape.Kind() != reflect.Slice {
			return errors.New("unexpected JSON array")
		}
		for dec.More() {
			if err := checkValue(dec, shape.Elem(), depth+1); err != nil {
				return err
			}
		}
		end, err := dec.Token()
		if err != nil || end != json.Delim(']') {
			return errors.New("invalid JSON array")
		}
	default:
		if _, ok := token.(json.Delim); ok {
			return errors.New("unexpected JSON delimiter")
		}
	}
	return nil
}
