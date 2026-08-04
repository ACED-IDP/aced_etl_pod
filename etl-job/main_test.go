package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestInputDataRetainsAPIEndpoint(t *testing.T) {
	var input inputData
	if err := json.Unmarshal([]byte(`{"APIEndpoint":"https://caliper-training.ohsu.edu"}`), &input); err != nil {
		t.Fatal(err)
	}
	if input.APIEndpoint != "https://caliper-training.ohsu.edu" {
		t.Fatalf("APIEndpoint = %q, want %q", input.APIEndpoint, "https://caliper-training.ohsu.edu")
	}
}

func TestSplitProjectID(t *testing.T) {
	program, project, err := splitProjectID("program-project")
	if err != nil || program != "program" || project != "project" {
		t.Fatalf("splitProjectID() = %q, %q, %v", program, project, err)
	}
	for _, value := range []string{"", "program", "-project", "program-", "a-b-c"} {
		if _, _, err := splitProjectID(value); err == nil {
			t.Fatalf("splitProjectID(%q) unexpectedly succeeded", value)
		}
	}
}

func TestRepoName(t *testing.T) {
	for input, want := range map[string]string{
		"https://github.com/example/study.git": "study",
		"github.com/example/study":             "study",
		"https://github.com/example/study/":    "study",
	} {
		if got := repoName(input); got != want {
			t.Fatalf("repoName(%q) = %q, want %q", input, got, want)
		}
	}
}

func TestListMatchingMissingDirectoryIsEmpty(t *testing.T) {
	files, err := listMatching(filepath.Join(t.TempDir(), "missing"), ".ndjson")
	if err != nil {
		t.Fatal(err)
	}
	if len(files) != 0 {
		t.Fatalf("listMatching() = %v, want empty", files)
	}
}

func TestPutResourceMultipartContract(t *testing.T) {
	client := &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		if r.Method != http.MethodPut || r.URL.Path != "/api/v1/projects/program-project/resources/Patient" {
			return nil, fmt.Errorf("unexpected request: %s %s", r.Method, r.URL.Path)
		}
		if got := r.Header.Get("Authorization"); got != "bearer token" {
			return nil, fmt.Errorf("unexpected authorization header: %q", got)
		}
		if err := r.ParseMultipartForm(1024 * 1024); err != nil {
			return nil, fmt.Errorf("parse multipart form: %v", err)
		}
		if got := r.FormValue("auth_resource_path"); got != "/programs/program/projects/project" {
			return nil, fmt.Errorf("unexpected auth resource path: %q", got)
		}
		file, header, err := r.FormFile("file")
		if err != nil {
			return nil, fmt.Errorf("read upload: %v", err)
		}
		defer file.Close()
		if header.Filename != "Patient.ndjson" {
			return nil, fmt.Errorf("unexpected filename: %q", header.Filename)
		}
		content, err := io.ReadAll(file)
		if err != nil {
			return nil, err
		}
		if string(content) != "{\"resourceType\":\"Patient\"}\n" {
			return nil, fmt.Errorf("unexpected upload content: %q", content)
		}
		return &http.Response{StatusCode: http.StatusOK, Status: "200 OK", Header: make(http.Header), Body: io.NopCloser(strings.NewReader(`{"ok":true}`))}, nil
	})}

	path := filepath.Join(t.TempDir(), "Patient.ndjson")
	if err := os.WriteFile(path, []byte("{\"resourceType\":\"Patient\"}\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	j := &job{
		ctx:        context.Background(),
		token:      "token",
		loomURL:    "https://loom.example",
		program:    "program",
		project:    "project",
		projectID:  "program-project",
		output:     &outputData{},
		httpClient: client,
	}
	if err := j.putResource("Patient", path); err != nil {
		t.Fatal(err)
	}
}

func TestOutputOmitsEmptyLogs(t *testing.T) {
	encoded, err := json.Marshal(outputData{User: "user", Files: []string{"Patient.ndjson"}})
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(encoded), "logs") {
		t.Fatalf("successful output contains buffered logs: %s", encoded)
	}
}

func TestValidateDocumentReferences(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "DocumentReference.ndjson")
	contents := "{\"resourceType\":\"DocumentReference\",\"id\":\"keep\",\"description\":\"authored\"}\n" +
		"{\"resourceType\":\"DocumentReference\",\"id\":\"keep\",\"description\":\"generated\"}\n"
	if err := os.WriteFile(path, []byte(contents), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := validateDocumentReferences(dir); err == nil || !strings.Contains(err.Error(), "duplicate DocumentReference id") {
		t.Fatalf("validateDocumentReferences() error = %v", err)
	}
}

func TestGraphQLPayloadContainsRecipeBindings(t *testing.T) {
	t.Setenv("LOOM_RECIPE_NAME", "")
	requests := 0
	client := &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		requests++
		if r.URL.Path != "/graphql/graph" || r.Method != http.MethodPost {
			return nil, fmt.Errorf("unexpected GraphQL request: %s %s", r.Method, r.URL.Path)
		}
		var payload struct {
			Query     string `json:"query"`
			Variables struct {
				Input struct {
					Name     string `json:"name"`
					Bindings struct {
						Project           string   `json:"project"`
						AuthResourcePaths []string `json:"authResourcePaths"`
					} `json:"bindings"`
				} `json:"input"`
			} `json:"variables"`
		}
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			return nil, err
		}
		if strings.Contains(payload.Query, "materializeDataframeRecipeBundle") {
			input := payload.Variables.Input
			if input.Name != "calypr-meta-default" || input.Bindings.Project != "program-project" || len(input.Bindings.AuthResourcePaths) != 1 || input.Bindings.AuthResourcePaths[0] != "/programs/program/projects/project" {
				return nil, fmt.Errorf("unexpected materialization input: %+v", input)
			}
			return &http.Response{StatusCode: http.StatusOK, Status: "200 OK", Header: http.Header{"Content-Type": []string{"application/json"}}, Body: io.NopCloser(strings.NewReader(`{"data":{"materializeDataframeRecipeBundle":{"id":"exec-1","state":"READY","name":"calypr-meta-default"}}}`))}, nil
		}
		if strings.Contains(payload.Query, "dataframeRecipeExecution") {
			return &http.Response{StatusCode: http.StatusOK, Status: "200 OK", Header: http.Header{"Content-Type": []string{"application/json"}}, Body: io.NopCloser(strings.NewReader(`{"data":{"dataframeRecipeExecution":{"id":"exec-1","state":"READY","error":null}}}`))}, nil
		}
		return nil, fmt.Errorf("unexpected GraphQL query: %q", payload.Query)
	})}
	j := &job{
		ctx:        context.Background(),
		token:      "token",
		loomURL:    "https://loom.example",
		program:    "program",
		project:    "project",
		projectID:  "program-project",
		httpClient: client,
	}
	if err := j.materialize(); err != nil {
		t.Fatal(err)
	}
	if requests != 2 {
		t.Fatalf("GraphQL requests = %d, want 2", requests)
	}
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) {
	return f(r)
}
