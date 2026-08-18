package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/calypr/forge/metadata"
)

func TestLogMissingSyfonRecordsPrettyPrintsEachIssue(t *testing.T) {
	var output bytes.Buffer
	old := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(&output, nil)))
	t.Cleanup(func() { slog.SetDefault(old) })

	logMissingSyfonRecords([]metadata.ReconcileIssue{
		{SHA256: "aaaa", Paths: []string{"data/a.txt"}},
		{SHA256: "bbbb", Paths: []string{"data/b.txt", "mirror/b.txt"}},
	})

	got := output.String()
	for _, want := range []string{"count=2", "item=1/2", "sha256=aaaa", "paths=data/a.txt", "item=2/2", "sha256=bbbb", `paths="data/b.txt, mirror/b.txt"`} {
		if !strings.Contains(got, want) {
			t.Errorf("warning output missing %q:\n%s", want, got)
		}
	}
}

func TestInputDataRetainsAPIEndpoint(t *testing.T) {
	var input inputData
	if err := json.Unmarshal([]byte(`{"APIEndpoint":"https://caliper-training.ohsu.edu"}`), &input); err != nil {
		t.Fatal(err)
	}
	if input.APIEndpoint != "https://caliper-training.ohsu.edu" {
		t.Fatalf("APIEndpoint = %q, want %q", input.APIEndpoint, "https://caliper-training.ohsu.edu")
	}
}

func TestValidateInputAllowsEmptyBucket(t *testing.T) {
	err := validateInput(inputData{
		GHUserName:   "user",
		GHToken:      "token",
		GHCommitHash: "commit",
		GHRepoURL:    "github.com/example/repository",
		Profile:      "dev",
		APIEndpoint:  "https://calypr-dev.example",
	})
	if err != nil {
		t.Fatalf("validateInput() rejected an empty bucket: %v", err)
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

func TestRedactInputPacket(t *testing.T) {
	got := redactInputPacket(`{"projectId":"program-project","ghToken":"secret","bucketName":"bucket"}`)
	if !strings.Contains(got, `"projectId":"program-project"`) {
		t.Fatalf("redacted packet lost projectId: %s", got)
	}
	if strings.Contains(got, "secret") || !strings.Contains(got, "[REDACTED]") {
		t.Fatalf("redacted packet exposed token: %s", got)
	}
}

func TestLoomGenerationDefaultsToGitCommit(t *testing.T) {
	now := time.Date(2026, time.August, 12, 18, 30, 45, 123456789, time.UTC)
	input := inputData{GHCommitHash: " commit-a "}
	if got := loomGeneration(input, now); got != "commit-a" {
		t.Fatalf("loomGeneration() = %q, want %q", got, "commit-a")
	}
}

func TestLoomGenerationForceRefreshCreatesFreshImmutableGeneration(t *testing.T) {
	now := time.Date(2026, time.August, 12, 18, 30, 45, 123456789, time.FixedZone("PDT", -7*60*60))
	input := inputData{GHCommitHash: "commit-a", ForceLoomRefresh: true}
	if got, want := loomGeneration(input, now), "commit-a-refresh-20260813T013045.123456789Z"; got != want {
		t.Fatalf("loomGeneration() = %q, want %q", got, want)
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

func TestUploadMetadataUsesExistingMultipartGenerationContract(t *testing.T) {
	client := &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		if r.Method != http.MethodPost || r.URL.Path != "/api/v1/datasets/program-project/generations/commit" {
			return nil, fmt.Errorf("unexpected request: %s %s", r.Method, r.URL.Path)
		}
		if got := r.Header.Get("Authorization"); got != "bearer token" {
			return nil, fmt.Errorf("unexpected authorization header: %q", got)
		}
		if !strings.HasPrefix(r.Header.Get("Content-Type"), "multipart/form-data;") {
			return nil, fmt.Errorf("unexpected content type: %q", r.Header.Get("Content-Type"))
		}
		if err := r.ParseMultipartForm(1024 * 1024); err != nil {
			return nil, err
		}
		if r.FormValue("project") != "program-project" || r.FormValue("generation") != "commit" || r.FormValue("auth_resource_path") != "/programs/program/projects/project" || r.FormValue("defer_activation") != "true" {
			return nil, fmt.Errorf("unexpected snapshot fields: project=%q generation=%q auth=%q defer=%q", r.FormValue("project"), r.FormValue("generation"), r.FormValue("auth_resource_path"), r.FormValue("defer_activation"))
		}
		files := r.MultipartForm.File["file"]
		if len(files) != 1 || files[0].Filename != "Patient.ndjson" {
			return nil, fmt.Errorf("unexpected multipart files: %#v", files)
		}
		file, err := files[0].Open()
		if err != nil {
			return nil, err
		}
		content, err := io.ReadAll(file)
		file.Close()
		if err != nil || string(content) != "{\"resourceType\":\"Patient\"}\n" {
			return nil, fmt.Errorf("unexpected upload content: %q (err=%v)", content, err)
		}
		return &http.Response{StatusCode: http.StatusOK, Status: "200 OK", Header: make(http.Header), Body: io.NopCloser(strings.NewReader(`{"state":"STAGED"}`))}, nil
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
		input:      inputData{GHCommitHash: "commit"},
		output:     &outputData{},
		httpClient: client,
	}
	if err := j.uploadMetadata(filepath.Dir(path)); err != nil {
		t.Fatal(err)
	}
	if len(j.output.Files) != 1 || j.output.Files[0] != path {
		t.Fatalf("uploaded files = %v", j.output.Files)
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

func TestSnapshotMaterializationAndReleaseContract(t *testing.T) {
	t.Setenv("LOOM_RECIPE_NAME", "calypr-meta-default")
	t.Setenv("LOOM_TRANSLATION_VERSION", "deployed-version")
	t.Setenv("LOOM_RECIPE_OUTPUTS", "DocumentReference")
	dir := t.TempDir()
	path := filepath.Join(dir, "DocumentReference.ndjson")
	if err := os.WriteFile(path, []byte("{\"resourceType\":\"DocumentReference\"}\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	client := &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		headers := http.Header{"Content-Type": []string{"application/json"}}
		jsonResponse := func(body string) *http.Response {
			return &http.Response{StatusCode: http.StatusOK, Status: "200 OK", Header: headers, Body: io.NopCloser(strings.NewReader(body))}
		}
		switch {
		case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/datasets/program-project/generations/commit"):
			if !strings.HasPrefix(r.Header.Get("Content-Type"), "multipart/form-data;") {
				return nil, errors.New("snapshot upload is not multipart")
			}
			if err := r.ParseMultipartForm(1024 * 1024); err != nil {
				return nil, err
			}
			if r.FormValue("project") != "program-project" || r.FormValue("generation") != "commit" {
				return nil, fmt.Errorf("unexpected snapshot fields: %q %q", r.FormValue("project"), r.FormValue("generation"))
			}
			return jsonResponse(`{"state":"STAGED"}`), nil
		case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/generations/commit/activate"):
			if r.URL.Query().Get("dataframe_execution_id") != "exec-1" || r.URL.Query().Get("auth_resource_path") != "/programs/program/projects/project" {
				return nil, fmt.Errorf("unexpected activation query: %s", r.URL.RawQuery)
			}
			return jsonResponse(`{"activated":true}`), nil
		case r.URL.Path == "/graphql/graph":
			var payload struct {
				Query     string         `json:"query"`
				Variables map[string]any `json:"variables"`
			}
			if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
				return nil, err
			}
			if strings.Contains(payload.Query, "validateDataframeRecipe") {
				if !strings.HasPrefix(strings.TrimSpace(payload.Query), "mutation") {
					return nil, fmt.Errorf("recipe validation must be a mutation: %s", payload.Query)
				}
				return jsonResponse(`{"data":{"validateDataframeRecipe":{"name":"calypr-meta-default","recipeDigest":"recipe","translationVersion":"deployed-version","outputs":[{"name":"DocumentReference"}]}}}`), nil
			}
			return jsonResponse(`{"data":{"materializeDataframeRecipeBundle":{"id":"exec-1","name":"calypr-meta-default","state":"READY","sourceGeneration":"commit","outputs":[{"name":"DocumentReference","state":"READY"}]}}}`), nil
		default:
			return nil, fmt.Errorf("unexpected request: %s %s", r.Method, r.URL.Path)
		}
	})}
	j := &job{ctx: context.Background(), token: "token", loomURL: "https://loom.example", program: "program", project: "project", projectID: "program-project", input: inputData{GHCommitHash: "commit"}, output: &outputData{}, httpClient: client}
	if err := j.snapshotAndActivate(dir); err != nil {
		t.Fatal(err)
	}
	if len(j.output.Files) != 1 || j.output.Files[0] != path {
		t.Fatalf("uploaded files = %v", j.output.Files)
	}
}

func TestSnapshotDoesNotActivateAfterRecipeValidationFailure(t *testing.T) {
	t.Setenv("LOOM_RECIPE_OUTPUTS", "DocumentReference")
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "DocumentReference.ndjson"), []byte("{}\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	activationCalls := 0
	client := &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		if strings.HasSuffix(r.URL.Path, "/activate") {
			activationCalls++
			return jsonHTTPResponse(`{"activated":true}`), nil
		}
		if strings.HasSuffix(r.URL.Path, "/generations/commit") {
			return jsonHTTPResponse(`{"state":"STAGED"}`), nil
		}
		if r.URL.Path == "/graphql/graph" {
			return jsonHTTPResponse(`{"errors":[{"message":"recipe validation failed","extensions":{"code":"RECIPE_INVALID","retryable":false}}]}`), nil
		}
		return nil, fmt.Errorf("unexpected request: %s %s", r.Method, r.URL.Path)
	})}
	j := testJob(client)
	if err := j.snapshotAndActivate(dir); err == nil {
		t.Fatal("snapshotAndActivate unexpectedly succeeded")
	}
	if activationCalls != 0 {
		t.Fatalf("activation calls = %d after validation failure, want 0", activationCalls)
	}
}

func TestSnapshotDoesNotActivateAfterMaterializationFailure(t *testing.T) {
	t.Setenv("LOOM_RECIPE_OUTPUTS", "DocumentReference")
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "DocumentReference.ndjson"), []byte("{}\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	activationCalls := 0
	client := &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		if strings.HasSuffix(r.URL.Path, "/activate") {
			activationCalls++
			return jsonHTTPResponse(`{"activated":true}`), nil
		}
		if strings.HasSuffix(r.URL.Path, "/generations/commit") {
			return jsonHTTPResponse(`{"state":"STAGED"}`), nil
		}
		if r.URL.Path == "/graphql/graph" {
			var payload struct {
				Query string `json:"query"`
			}
			if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
				return nil, err
			}
			if strings.Contains(payload.Query, "validateDataframeRecipe") {
				return jsonHTTPResponse(`{"data":{"validateDataframeRecipe":{"name":"calypr-meta-default","translationVersion":"deployed-version","outputs":[{"name":"DocumentReference"}]}}}`), nil
			}
			return jsonHTTPResponse(`{"data":{"materializeDataframeRecipeBundle":{"id":"exec-failed","name":"calypr-meta-default","state":"FAILED","sourceGeneration":"commit","error":"output failed","errorCode":"OUTPUT_FAILED"}}}`), nil
		}
		return nil, fmt.Errorf("unexpected request: %s %s", r.Method, r.URL.Path)
	})}
	j := testJob(client)
	if err := j.snapshotAndActivate(dir); err == nil {
		t.Fatal("snapshotAndActivate unexpectedly succeeded")
	}
	if activationCalls != 0 {
		t.Fatalf("activation calls = %d after materialization failure, want 0", activationCalls)
	}
}

func TestReadRepositoryExplorerDefinitionRequiresExactV2(t *testing.T) {
	dir := t.TempDir()
	config := `{"apiVersion":"loom.calypr.org/explorer-config/v2","kind":"ExplorerConfig","project":"program-project","explorer":{"id":"default","title":"Default","management":"repository"},"recipe":{"recipeSchemaVersion":1,"name":"recipe","translationVersion":"v1","outputs":[{"name":"Patient","rootResourceType":"Patient","rowGrain":"patient","fields":[{"name":"id","expr":{"select":"root.id"}}]}]}}`
	if err := os.WriteFile(filepath.Join(dir, "program-project.json"), []byte(config), 0o600); err != nil {
		t.Fatal(err)
	}
	j := testJob(http.DefaultClient)
	got, present, err := j.readRepositoryExplorerDefinition(dir)
	if err != nil || !present || string(got) != config {
		t.Fatalf("definition = %s, present=%v, err=%v", got, present, err)
	}
	if err := os.WriteFile(filepath.Join(dir, "program-project.json"), []byte(`{"explorerConfig":[]}`), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, _, err := j.readRepositoryExplorerDefinition(dir); err == nil {
		t.Fatal("legacy document accepted")
	}
	withPresentation := strings.TrimSuffix(config, "}") + `,"views":[{"id":"patient","title":"Patients","output":"Patient","table":{"columns":[{"column":"id","visible":true}]}}]}`
	if err := os.WriteFile(filepath.Join(dir, "program-project.json"), []byte(withPresentation), 0o600); err != nil {
		t.Fatal(err)
	}
	if got, present, err := j.readRepositoryExplorerDefinition(dir); err != nil || !present || string(got) != withPresentation {
		t.Fatalf("repository presentation = %s, present=%v, err=%v", got, present, err)
	}
}

func TestRepositoryDeployUsesV2RESTEndpoint(t *testing.T) {
	client := &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		if r.URL.Path != "/api/v1/projects/program-project/generations/commit/explorer-config" || r.Method != http.MethodPost {
			return nil, fmt.Errorf("unexpected request %s", r.URL.Path)
		}
		if r.Header.Get("X-Loom-Source-Commit") != "commit" {
			return nil, fmt.Errorf("missing source commit")
		}
		return jsonHTTPResponse(`{"project":"program-project","generation":"commit","activated":true,"executionId":"execution-1","recipe":"explorer_program-project_default","translationVersion":"repository-commit"}`), nil
	})}
	j := testJob(client)
	if err := j.deployRepositoryExplorerConfigV2(json.RawMessage(`{"apiVersion":"loom.calypr.org/explorer-config/v2"}`)); err != nil {
		t.Fatal(err)
	}
}

func TestRepositoryDeployRejectsMissingExecutableIdentity(t *testing.T) {
	client := &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		return jsonHTTPResponse(`{"project":"program-project","generation":"commit","activated":true,"executionId":"execution-1"}`), nil
	})}
	j := testJob(client)
	if err := j.deployRepositoryExplorerConfigV2(json.RawMessage(`{"apiVersion":"loom.calypr.org/explorer-config/v2"}`)); err == nil || !strings.Contains(err.Error(), "inconsistent deployment identity") {
		t.Fatalf("error = %v, want inconsistent deployment identity", err)
	}
}

func TestRepositoryDeployRejectsWrongExecutableIdentity(t *testing.T) {
	client := &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		return jsonHTTPResponse(`{"project":"program-project","generation":"commit","activated":true,"executionId":"execution-1","recipe":"calypr-meta-default","translationVersion":"v1"}`), nil
	})}
	j := testJob(client)
	if err := j.deployRepositoryExplorerConfigV2(json.RawMessage(`{"apiVersion":"loom.calypr.org/explorer-config/v2"}`)); err == nil || !strings.Contains(err.Error(), "inconsistent deployment identity") {
		t.Fatalf("error = %v, want inconsistent deployment identity", err)
	}
}

func testJob(client *http.Client) *job {
	return &job{
		ctx:        context.Background(),
		token:      "token",
		loomURL:    "https://loom.example",
		program:    "program",
		project:    "project",
		projectID:  "program-project",
		input:      inputData{GHCommitHash: "commit"},
		output:     &outputData{},
		httpClient: client,
	}
}

func jsonHTTPResponse(body string) *http.Response {
	return &http.Response{StatusCode: http.StatusOK, Status: "200 OK", Header: http.Header{"Content-Type": []string{"application/json"}}, Body: io.NopCloser(strings.NewReader(body))}
}

func TestValidateRecipeAllowsAdditionalOutputs(t *testing.T) {
	client := &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		return jsonHTTPResponse(`{"data":{"validateDataframeRecipe":{"name":"calypr-meta-default","recipeDigest":"recipe","translationVersion":"version","outputs":[{"name":"DocumentReference"},{"name":"GroupMember"},{"name":"MedicationAdministration"},{"name":"ResearchSubject"},{"name":"Specimen"}]}}}`), nil
	})}
	j := testJob(client)
	selectors := []dataframeSelector{
		{Recipe: "calypr-meta-default", Output: "DocumentReference"},
		{Recipe: "calypr-meta-default", Output: "ResearchSubject"},
		{Recipe: "calypr-meta-default", Output: "Specimen"},
	}
	if _, err := j.validateRecipe("commit", selectors); err != nil {
		t.Fatalf("validateRecipe() rejected additional recipe outputs: %v", err)
	}
}

func TestValidateRecipeRejectsMissingRequiredOutput(t *testing.T) {
	client := &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		return jsonHTTPResponse(`{"data":{"validateDataframeRecipe":{"name":"calypr-meta-default","recipeDigest":"recipe","translationVersion":"version","outputs":[{"name":"DocumentReference"}]}}}`), nil
	})}
	j := testJob(client)
	selectors := []dataframeSelector{
		{Recipe: "calypr-meta-default", Output: "DocumentReference"},
		{Recipe: "calypr-meta-default", Output: "ResearchSubject"},
	}
	if _, err := j.validateRecipe("commit", selectors); err == nil || !strings.Contains(err.Error(), `missing required output "ResearchSubject"`) {
		t.Fatalf("validateRecipe() error = %v", err)
	}
}

func TestGraphQLPayloadContainsRecipeBindings(t *testing.T) {
	requests := 0
	client := &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		requests++
		if r.URL.Path != "/graphql/graph" || r.Method != http.MethodPost {
			return nil, fmt.Errorf("unexpected GraphQL request: %s %s", r.Method, r.URL.Path)
		}
		var payload struct {
			Query     string         `json:"query"`
			Variables map[string]any `json:"variables"`
		}
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			return nil, err
		}
		if !strings.Contains(payload.Query, "materializeDataframeRecipeBundle") {
			return nil, fmt.Errorf("unexpected GraphQL query: %q", payload.Query)
		}
		encoded, _ := json.Marshal(payload.Variables)
		if !strings.Contains(string(encoded), `"name":"calypr-meta-default"`) || !strings.Contains(string(encoded), `"datasetGeneration":"commit"`) {
			return nil, fmt.Errorf("unexpected materialization variables: %s", encoded)
		}
		return &http.Response{StatusCode: http.StatusOK, Status: "200 OK", Header: http.Header{"Content-Type": []string{"application/json"}}, Body: io.NopCloser(strings.NewReader(`{"data":{"materializeDataframeRecipeBundle":{"id":"exec-1","name":"calypr-meta-default","state":"READY","sourceGeneration":"commit"}}}`))}, nil
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
	if _, err := j.materializeRecipe("commit", recipeValidation{Name: "calypr-meta-default", TranslationVersion: "deployed-version", Outputs: []recipeValidationOutput{{Name: "DocumentReference"}}}); err != nil {
		t.Fatal(err)
	}
	if requests != 1 {
		t.Fatalf("GraphQL requests = %d, want 1", requests)
	}
}

func TestGraphQLPreservesLoomErrorDiagnostics(t *testing.T) {
	retryable := false
	j := &job{
		ctx:     context.Background(),
		loomURL: "https://loom.example",
		httpClient: &http.Client{Transport: roundTripFunc(func(*http.Request) (*http.Response, error) {
			return &http.Response{
				StatusCode: http.StatusOK,
				Status:     "200 OK",
				Header:     http.Header{"Content-Type": []string{"application/json"}},
				Body:       io.NopCloser(strings.NewReader(`{"errors":[{"message":"internal server error","extensions":{"code":"INTERNAL_ERROR","requestId":"request-123","retryable":false,"fieldPath":["outputs","0"]}}]}`)),
			}, nil
		})},
	}
	err := j.graphql(`query { ignored }`, nil, &struct{}{})
	if err == nil {
		t.Fatal("graphql() unexpectedly succeeded")
	}
	var got loomGraphQLError
	if !errors.As(err, &got) {
		t.Fatalf("error type = %T, want loomGraphQLError", err)
	}
	if got.Message != "internal server error" || got.Code != "INTERNAL_ERROR" || got.RequestID != "request-123" || got.Retryable == nil || *got.Retryable != retryable || strings.Join(got.FieldPath, ".") != "outputs.0" {
		t.Fatalf("unexpected Loom GraphQL error: %#v", got)
	}
	for _, want := range []string{"code=INTERNAL_ERROR", "request_id=request-123", "retryable=false", "field_path=outputs.0"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("error %q missing %q", err, want)
		}
	}
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) {
	return f(r)
}
