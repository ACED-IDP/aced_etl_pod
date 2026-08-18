package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"mime/multipart"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/calypr/forge/metadata"
	gitdrs "github.com/calypr/git-drs/client"
)

const (
	metaDir                   = "META"
	configDir                 = "CONFIG"
	requestTimeout            = 5 * time.Minute
	uploadTimeout             = time.Hour
	recipePollInterval        = 2 * time.Second
	recipePollTimeout         = 30 * time.Minute
	defaultRecipeName         = "calypr-meta-default"
	defaultRepositoryDir      = "/root/repo"
	operationProgressInterval = 30 * time.Second
	maxRequestAttempts        = 3
	requestRetryDelay         = 500 * time.Millisecond
)

var workingDirectoryMu sync.Mutex

type inputData struct {
	Method           string `json:"method"`
	ProjectID        string `json:"projectId"`
	GHUserName       string `json:"ghUserName"`
	GHToken          string `json:"ghToken"`
	GHCommitHash     string `json:"ghCommitHash"`
	GHRepoURL        string `json:"ghRepoUrl"`
	BucketName       string `json:"bucketName"`
	Profile          string `json:"profile"`
	APIEndpoint      string `json:"APIEndpoint"`
	ForceLoomRefresh bool   `json:"forceLoomRefresh,omitempty"`
}

type outputData struct {
	User  string   `json:"user"`
	Files []string `json:"files"`
	Logs  []string `json:"logs,omitempty"`
}

type job struct {
	ctx        context.Context
	token      string
	hostname   string
	loomURL    string
	program    string
	project    string
	projectID  string
	generation string
	input      inputData
	output     *outputData
	httpClient *http.Client
}

type dataframeSelector struct {
	Recipe             string `json:"recipe"`
	TranslationVersion string `json:"translationVersion"`
	Output             string `json:"output"`
}

var defaultDataframeOutputs = []string{
	"DocumentReference",
	"ResearchSubject",
	"MedicationAdministration",
	"Specimen",
	"GroupMember",
}

type httpError struct {
	Code      string         `json:"code"`
	Message   string         `json:"message"`
	Details   map[string]any `json:"details"`
	Retryable bool           `json:"retryable"`
	RequestID string         `json:"requestId"`
}

func (e httpError) Error() string {
	message := strings.TrimSpace(e.Message)
	if message == "" {
		message = "Loom HTTP request failed"
	}
	parts := make([]string, 0, 3)
	if e.Code != "" {
		parts = append(parts, "code="+e.Code)
	}
	if e.RequestID != "" {
		parts = append(parts, "request_id="+e.RequestID)
	}
	if len(e.Details) > 0 {
		parts = append(parts, fmt.Sprintf("details=%v", e.Details))
	}
	parts = append(parts, "retryable="+strconv.FormatBool(e.Retryable))
	return "Loom HTTP error: " + message + " (" + strings.Join(parts, ", ") + ")"
}

// loomGraphQLError preserves the safe diagnostic fields returned by Loom's
// GraphQL error envelope. The server deliberately keeps implementation causes
// out of the response; requestID lets an operator correlate this failure with
// Loom's structured server log instead.
type loomGraphQLError struct {
	Message   string
	Code      string
	RequestID string
	Retryable *bool
	FieldPath []string
	Details   map[string]any
}

func (e loomGraphQLError) Error() string {
	message := strings.TrimSpace(e.Message)
	if message == "" {
		message = "GraphQL request failed"
	}
	parts := make([]string, 0, 4)
	if e.Code != "" {
		parts = append(parts, "code="+e.Code)
	}
	if e.RequestID != "" {
		parts = append(parts, "request_id="+e.RequestID)
	}
	if e.Retryable != nil {
		parts = append(parts, "retryable="+strconv.FormatBool(*e.Retryable))
	}
	if len(e.FieldPath) > 0 {
		parts = append(parts, "field_path="+strings.Join(e.FieldPath, "."))
	}
	if len(e.Details) > 0 {
		parts = append(parts, fmt.Sprintf("details=%v", e.Details))
	}
	if len(parts) == 0 {
		return "Loom GraphQL error: " + message
	}
	return "Loom GraphQL error: " + message + " (" + strings.Join(parts, ", ") + ")"
}

func main() {
	logger := slog.New(slog.NewTextHandler(os.Stdout, nil))
	slog.SetDefault(logger)
	ctx := context.Background()
	output := &outputData{Files: []string{}, Logs: []string{}}

	token, err := requiredEnv("ACCESS_TOKEN")
	if err != nil {
		fatalOutput(output, err)
		return
	}
	hostname, err := requiredEnv("GEN3_HOSTNAME")
	if err != nil {
		fatalOutput(output, err)
		return
	}
	hostname = "https://" + strings.TrimRight(hostname, "/")

	rawInput, err := requiredEnv("INPUT_DATA")
	if err != nil {
		fatalOutput(output, err)
		return
	}
	output.Logs = append(output.Logs, "received INPUT_DATA="+redactInputPacket(rawInput))

	input, err := parseInput(rawInput)
	if err != nil {
		fatalOutput(output, err)
		return
	}
	program, project, err := splitProjectID(input.ProjectID)
	if err != nil {
		fatalOutput(output, err)
		return
	}

	var user map[string]any
	if err := runOperation("retrieve user profile", func() error {
		var err error
		user, err = fetchUser(ctx, hostname, token)
		return err
	}); err != nil {
		fatalOutput(output, fmt.Errorf("retrieve user info: %w", err))
		return
	}
	output.User = stringValue(user["email"])
	logger.Info("starting ETL", "method", input.Method, "project_id", input.ProjectID, "user", output.User)

	j := &job{
		ctx:        ctx,
		token:      token,
		hostname:   hostname,
		loomURL:    strings.TrimRight(envOr("LOOM_URL", hostname+"/loom"), "/"),
		program:    program,
		project:    project,
		projectID:  input.ProjectID,
		generation: loomGeneration(input, time.Now()),
		input:      input,
		output:     output,
		// Individual Loom calls apply their own request or upload deadline. A
		// client-wide timeout would truncate large NDJSON uploads before the
		// uploadTimeout context can take effect.
		httpClient: &http.Client{Timeout: 0},
	}
	logger.Info("resolved Loom generation", "source_commit", input.GHCommitHash, "generation", j.generation, "forced_refresh", input.ForceLoomRefresh)

	if strings.EqualFold(input.Method, "put") {
		err = j.put()
	} else if strings.EqualFold(input.Method, "delete") {
		err = errors.New("delete is not supported by the Loom bulk API")
	} else {
		err = fmt.Errorf("unknown method %q", input.Method)
	}
	if err != nil {
		fatalOutput(output, err)
		return
	}
	writeOutput(output)
}

func (j *job) put() error {
	if err := runOperation("validate job input", func() error {
		return validateInput(j.input)
	}); err != nil {
		return err
	}

	loadPath := filepath.Join(defaultRepositoryDir, j.project)
	workspaceDir := filepath.Dir(loadPath)
	if err := runOperation("create repository workspace", func() error {
		return os.MkdirAll(workspaceDir, 0o755)
	}); err != nil {
		return fmt.Errorf("create repository workspace: %w", err)
	}
	targetDir := filepath.Join(workspaceDir, repoName(j.input.GHRepoURL))
	defer func() {
		slog.Info("ETL operation started", "operation", "clean up repository workspace")
		_ = os.RemoveAll(loadPath)
		_ = os.RemoveAll(targetDir)
		slog.Info("ETL operation completed", "operation", "clean up repository workspace")
	}()

	if err := runOperation("clone repository", func() error {
		return cloneRepository(j.ctx, j.input, workspaceDir)
	}, "repository", j.input.GHRepoURL); err != nil {
		return err
	}
	if err := runOperation("checkout requested commit", func() error {
		return checkoutRepository(j.ctx, targetDir, j.input.GHCommitHash)
	}, "commit", j.input.GHCommitHash); err != nil {
		return err
	}
	if j.input.Profile != "origin" {
		if err := runOperation("rename Git remote", func() error {
			return runGit(j.ctx, targetDir, "remote", "rename", "origin", j.input.Profile)
		}, "remote", j.input.Profile); err != nil {
			return fmt.Errorf("rename git remote: %w", err)
		}
	}

	if err := runOperation("initialize git-drs repository", func() error {
		return withWorkingDirectory(targetDir, func() error {
			if err := gitdrs.InitializeRepository(slog.Default()); err != nil {
				return fmt.Errorf("initialize git-drs: %w", err)
			}
			return nil
		})
	}); err != nil {
		return err
	}
	if err := runOperation("configure git-drs remote", func() error {
		return withWorkingDirectory(targetDir, func() error {
			if err := gitdrs.ConfigureGen3Remote(gitdrs.Gen3RemoteOptions{
				RemoteName: j.input.Profile,
				Token:      j.token,
				Bucket:     j.input.BucketName,
				Scope:      j.program + "/" + j.project,
				Logger:     slog.Default(),
			}); err != nil {
				return fmt.Errorf("configure git-drs remote: %w", err)
			}
			return nil
		})
	}, "scope", j.program+"/"+j.project); err != nil {
		return err
	}

	var metaFiles, configFiles []string
	if err := runOperation("discover repository inputs", func() error {
		var err error
		metaFiles, err = listNDJSON(filepath.Join(targetDir, metaDir))
		if err != nil {
			return fmt.Errorf("discover META files: %w", err)
		}
		configFiles, err = listJSON(filepath.Join(targetDir, configDir))
		if err != nil {
			return fmt.Errorf("discover CONFIG files: %w", err)
		}
		return nil
	}); err != nil {
		return err
	}
	slog.Info("repository inputs discovered", "metadata_files", len(metaFiles), "config_files", len(configFiles))
	if len(metaFiles) > 0 || len(configFiles) > 0 {
		drsEndpoint := strings.TrimRight(strings.TrimSpace(j.input.APIEndpoint), "/")
		if drsEndpoint == "" {
			return fmt.Errorf("Sower input APIEndpoint is required for Git-DRS hydration")
		}
		if err := runOperation("hydrate Git-DRS pointer files", func() error {
			return hydratePointers(j.ctx, drsEndpoint, j.token, j.program, j.project, targetDir, append(metaFiles, configFiles...))
		}, "files", len(metaFiles)+len(configFiles)); err != nil {
			return fmt.Errorf("pull repository files with git-drs: %w", err)
		}
	} else {
		slog.Info("no Git-DRS pointer files require hydration")
	}

	var reconciliation metadata.ReconcileReport
	if err := runOperation("reconcile Git metadata with Syfon", func() error {
		return withWorkingDirectory(targetDir, func() error {
			var err error
			reconciliation, err = metadata.ReconcileGitPointers(j.ctx, metadata.ReconcileOptions{
				RepositoryRoot: targetDir,
				GitRef:         j.input.GHCommitHash,
				FHIRDirectory:  filepath.Join(targetDir, metaDir),
				ProfileName:    j.input.Profile,
				GitRemoteName:  j.input.Profile,
			})
			return err
		})
	}); err != nil {
		return fmt.Errorf("generate forge metadata: %w", err)
	}
	message := fmt.Sprintf("Git/Syfon reconciliation: pointers=%d authored=%d matched=%d generated=%d missing_syfon=%d metadata_only=%d authored_without_sha=%d", reconciliation.GitPointers, reconciliation.AuthoredRows, reconciliation.MatchedRows, reconciliation.GeneratedRows, len(reconciliation.MissingSyfonRecords), len(reconciliation.MetadataOnlySHA256), reconciliation.AuthoredRowsWithoutSHA)
	slog.Info(message)
	logMissingSyfonRecords(reconciliation.MissingSyfonRecords)
	if len(reconciliation.MetadataOnlySHA256) > 0 {
		warning := fmt.Sprintf("WARNING: retained %d authored DocumentReference SHA256 values not present in the Git snapshot", len(reconciliation.MetadataOnlySHA256))
		slog.Warn(warning)
	}
	if err := runOperation("prepare generated metadata", func() error {
		if err := moveGeneratedMetadata(targetDir, loadPath); err != nil {
			return err
		}
		return validateDocumentReferences(loadPath)
	}); err != nil {
		return err
	}
	configPath := filepath.Join(targetDir, configDir)
	definition, present, err := j.readRepositoryExplorerDefinition(configPath)
	if err != nil {
		return err
	}
	if err := runOperation("stage FHIR metadata and deploy repository Explorer", func() error {
		if !present {
			return j.snapshotAndActivate(loadPath)
		}
		if err := j.uploadMetadata(loadPath); err != nil {
			return err
		}
		return j.deployRepositoryExplorerConfigV2(definition)
	}); err != nil {
		return err
	}
	return nil
}

func logMissingSyfonRecords(issues []metadata.ReconcileIssue) {
	if len(issues) == 0 {
		return
	}
	slog.Warn("Git pointers without scoped Syfon records; metadata was not generated for these files", "count", len(issues))
	for i, issue := range issues {
		paths := strings.Join(issue.Paths, ", ")
		if paths == "" {
			paths = "<unknown>"
		}
		slog.Warn("missing scoped Syfon record",
			"item", fmt.Sprintf("%d/%d", i+1, len(issues)),
			"sha256", issue.SHA256,
			"paths", paths,
		)
	}
}

func (j *job) uploadMetadata(dir string) error {
	files, err := listNDJSON(dir)
	if err != nil {
		return fmt.Errorf("discover generated metadata: %w", err)
	}
	if len(files) == 0 {
		return fmt.Errorf("no NDJSON files found in %s", dir)
	}
	sort.Strings(files)
	var body bytes.Buffer
	multipartWriter := multipart.NewWriter(&body)
	handles := make([]*os.File, 0, len(files))
	defer func() {
		for _, handle := range handles {
			_ = handle.Close()
		}
	}()
	for _, path := range files {
		handle, err := os.Open(path)
		if err != nil {
			return fmt.Errorf("open metadata %s: %w", filepath.Base(path), err)
		}
		handles = append(handles, handle)
		part, err := multipartWriter.CreateFormFile("file", filepath.Base(path))
		if err != nil {
			return err
		}
		if _, err := io.Copy(part, handle); err != nil {
			return fmt.Errorf("read metadata %s: %w", filepath.Base(path), err)
		}
	}
	if err := multipartWriter.WriteField("project", j.projectID); err != nil {
		return err
	}
	if err := multipartWriter.WriteField("generation", j.loomGeneration()); err != nil {
		return err
	}
	if err := multipartWriter.WriteField("auth_resource_path", fmt.Sprintf("/programs/%s/projects/%s", j.program, j.project)); err != nil {
		return err
	}
	if err := multipartWriter.WriteField("defer_activation", "true"); err != nil {
		return err
	}
	if err := multipartWriter.Close(); err != nil {
		return err
	}
	endpoint := fmt.Sprintf("%s/api/v1/datasets/%s/generations/%s", j.loomURL, url.PathEscape(j.projectID), url.PathEscape(j.loomGeneration()))
	slog.Info("uploading complete FHIR snapshot to Loom", "files", len(files), "project_id", j.projectID, "generation", j.loomGeneration(), "source_commit", j.input.GHCommitHash)
	for attempt := 1; attempt <= maxRequestAttempts; attempt++ {
		ctx, cancel := context.WithTimeout(j.ctx, uploadTimeout)
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(body.Bytes()))
		if err != nil {
			cancel()
			return err
		}
		req.Header.Set("Authorization", "bearer "+j.token)
		req.Header.Set("Content-Type", multipartWriter.FormDataContentType())
		resp, err := j.httpClient.Do(req)
		if err == nil && resp.StatusCode >= 200 && resp.StatusCode < 300 {
			resp.Body.Close()
			cancel()
			j.output.Files = append(j.output.Files, files...)
			return nil
		}
		if err == nil {
			err = responseError(resp)
			resp.Body.Close()
		}
		cancel()
		if attempt == maxRequestAttempts || !retryableLoomError(err) {
			return fmt.Errorf("load FHIR snapshot: %w", err)
		}
		if err := waitForRetry(j.ctx, attempt); err != nil {
			return err
		}
	}
	return errors.New("unreachable snapshot upload retry state")
}

func (j *job) materializeSnapshot(dir string, selectors []dataframeSelector) (string, recipeExecution, error) {
	generation := j.loomGeneration()
	if err := j.uploadMetadata(dir); err != nil {
		return "", recipeExecution{}, err
	}
	if len(selectors) == 0 {
		var err error
		selectors, err = configuredSelectors(nil)
		if err != nil {
			return "", recipeExecution{}, err
		}
	}
	validation, err := j.validateRecipe(generation, selectors)
	if err != nil {
		return "", recipeExecution{}, err
	}
	execution, err := j.materializeRecipe(generation, validation)
	if err != nil {
		return "", recipeExecution{}, err
	}
	if execution.ID == "" {
		return "", recipeExecution{}, errors.New("Loom recipe materialization returned no execution ID")
	}
	return generation, execution, nil
}

func loomGeneration(input inputData, now time.Time) string {
	commit := strings.TrimSpace(input.GHCommitHash)
	if !input.ForceLoomRefresh {
		return commit
	}
	return commit + "-refresh-" + now.UTC().Format("20060102T150405.000000000Z")
}

func (j *job) loomGeneration() string {
	if generation := strings.TrimSpace(j.generation); generation != "" {
		return generation
	}
	return strings.TrimSpace(j.input.GHCommitHash)
}

func (j *job) snapshotAndActivate(dir string) error {
	generation, execution, err := j.materializeSnapshot(dir, nil)
	if err != nil {
		return err
	}
	if err := j.activateGeneration(generation, execution.ID); err != nil {
		return fmt.Errorf("activate Loom generation: %w", err)
	}
	return nil
}

type explorerConfigV2 struct {
	APIVersion    string          `json:"apiVersion"`
	Kind          string          `json:"kind"`
	Project       string          `json:"project"`
	Explorer      explorerOwner   `json:"explorer"`
	Recipe        json.RawMessage `json:"recipe"`
	Views         json.RawMessage `json:"views"`
	SharedFilters json.RawMessage `json:"sharedFilters,omitempty"`
	FileActions   json.RawMessage `json:"fileActions,omitempty"`
}
type explorerOwner struct {
	ID          string `json:"id"`
	Title       string `json:"title"`
	Description string `json:"description,omitempty"`
	Management  string `json:"management"`
}

// readRepositoryExplorerDefinition accepts the repository V2 packet. It may
// be baseline-only or may include the complete ETL-authored presentation
// layer. Loom performs the authoritative validation and preserves the packet
// unchanged through publication.
func (j *job) readRepositoryExplorerDefinition(dir string) (json.RawMessage, bool, error) {
	path := filepath.Join(dir, j.projectID+".json")
	content, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, fmt.Errorf("read repository Explorer definition: %w", err)
	}
	decoder := json.NewDecoder(bytes.NewReader(content))
	decoder.DisallowUnknownFields()
	var definition explorerConfigV2
	if err := decoder.Decode(&definition); err != nil {
		return nil, false, fmt.Errorf("decode repository Explorer definition %s: %w", filepath.Base(path), err)
	}
	var trailing any
	if err := decoder.Decode(&trailing); err != io.EOF {
		return nil, false, fmt.Errorf("decode repository Explorer definition %s: trailing JSON", filepath.Base(path))
	}
	if definition.APIVersion != "loom.calypr.org/explorer-config/v2" || definition.Kind != "ExplorerConfig" || definition.Project != j.projectID || definition.Explorer.ID != "default" || definition.Explorer.Management != "repository" || strings.TrimSpace(definition.Explorer.Title) == "" || len(definition.Recipe) == 0 {
		return nil, false, fmt.Errorf("repository Explorer definition %s must be ExplorerConfigV2 for project %q", filepath.Base(path), j.projectID)
	}
	return json.RawMessage(content), true, nil
}

func (j *job) deployRepositoryExplorerConfigV2(definition json.RawMessage) error {
	endpoint := fmt.Sprintf("%s/api/v1/projects/%s/generations/%s/explorer-config", j.loomURL, url.PathEscape(j.projectID), url.PathEscape(j.loomGeneration()))
	endpoint += "?auth_resource_path=" + url.QueryEscape(fmt.Sprintf("/programs/%s/projects/%s", j.program, j.project))
	req, err := http.NewRequestWithContext(j.ctx, http.MethodPost, endpoint, bytes.NewReader(definition))
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "bearer "+j.token)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Loom-Source-Commit", j.input.GHCommitHash)
	resp, err := j.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("deploy ExplorerConfigV2: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("deploy ExplorerConfigV2: %w", responseError(resp))
	}
	var deployed struct {
		Project            string `json:"project"`
		Generation         string `json:"generation"`
		ExecutionID        string `json:"executionId"`
		Recipe             string `json:"recipe"`
		TranslationVersion string `json:"translationVersion"`
		Activated          bool   `json:"activated"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(&deployed); err != nil {
		return fmt.Errorf("deploy ExplorerConfigV2: decode Loom deployment response: %w", err)
	}
	expectedRecipe := "explorer_" + j.projectID + "_default"
	expectedTranslationVersion := "repository-" + j.input.GHCommitHash
	if !deployed.Activated || deployed.Project != j.projectID || deployed.Generation != j.loomGeneration() || strings.TrimSpace(deployed.ExecutionID) == "" || deployed.Recipe != expectedRecipe || deployed.TranslationVersion != expectedTranslationVersion {
		return fmt.Errorf("deploy ExplorerConfigV2: Loom returned an inconsistent deployment identity (project=%q generation=%q execution=%q recipe=%q translationVersion=%q activated=%t; expected project=%q generation=%q recipe=%q translationVersion=%q activated=true)", deployed.Project, deployed.Generation, deployed.ExecutionID, deployed.Recipe, deployed.TranslationVersion, deployed.Activated, j.projectID, j.loomGeneration(), expectedRecipe, expectedTranslationVersion)
	}
	slog.Info("deployed repository ExplorerConfigV2", "project_id", deployed.Project, "generation", deployed.Generation, "execution_id", deployed.ExecutionID, "recipe", deployed.Recipe, "translation_version", deployed.TranslationVersion, "activated", deployed.Activated)
	return nil
}

type recipeExecutionOutput struct {
	Name      string  `json:"name"`
	State     string  `json:"state"`
	Error     *string `json:"error"`
	ErrorCode string  `json:"errorCode"`
}

type recipeExecution struct {
	ID               string                  `json:"id"`
	State            string                  `json:"state"`
	SourceGeneration string                  `json:"sourceGeneration"`
	Error            *string                 `json:"error"`
	ErrorCode        string                  `json:"errorCode"`
	ErrorRetryable   *bool                   `json:"errorRetryable"`
	Phase            string                  `json:"phase"`
	Outputs          []recipeExecutionOutput `json:"outputs"`
}

type recipeValidationOutput struct {
	Name string `json:"name"`
}

type recipeValidation struct {
	Name               string                   `json:"name"`
	RecipeDigest       string                   `json:"recipeDigest"`
	TranslationVersion string                   `json:"translationVersion"`
	Outputs            []recipeValidationOutput `json:"outputs"`
}

func configuredSelectors(requiredOutputs []string) ([]dataframeSelector, error) {
	if raw := strings.TrimSpace(os.Getenv("LOOM_REQUIRED_DATAFRAME_SELECTORS")); raw != "" {
		var selectors []dataframeSelector
		if err := json.Unmarshal([]byte(raw), &selectors); err != nil {
			return nil, fmt.Errorf("parse LOOM_REQUIRED_DATAFRAME_SELECTORS: %w", err)
		}
		if len(selectors) == 0 {
			return nil, errors.New("LOOM_REQUIRED_DATAFRAME_SELECTORS must not be empty")
		}
		for _, selector := range selectors {
			if err := validateSelector(selector); err != nil {
				return nil, err
			}
		}
		return selectors, nil
	}
	recipe := envOr("LOOM_RECIPE_NAME", defaultRecipeName)
	// The deployed recipe document owns its translation version.  An optional
	// environment value is an assertion used to detect deployment drift; it is
	// never sent as a selector to Loom (the current API materializes a complete
	// registered recipe bundle).
	version := strings.TrimSpace(os.Getenv("LOOM_TRANSLATION_VERSION"))
	outputs := strings.TrimSpace(os.Getenv("LOOM_RECIPE_OUTPUTS"))
	if len(requiredOutputs) > 0 {
		outputs = strings.Join(requiredOutputs, ",")
	} else if outputs == "" {
		outputs = strings.Join(defaultDataframeOutputs, ",")
	}
	selectors := make([]dataframeSelector, 0)
	seen := make(map[string]struct{})
	for _, value := range strings.Split(outputs, ",") {
		output := strings.TrimSpace(value)
		if output == "" {
			continue
		}
		selector := dataframeSelector{Recipe: recipe, TranslationVersion: version, Output: output}
		if err := validateSelector(selector); err != nil {
			return nil, err
		}
		key := selector.Recipe + "\x00" + selector.TranslationVersion + "\x00" + selector.Output
		if _, ok := seen[key]; !ok {
			seen[key] = struct{}{}
			selectors = append(selectors, selector)
		}
	}
	if len(selectors) == 0 {
		return nil, errors.New("LOOM_RECIPE_OUTPUTS must contain at least one output")
	}
	return selectors, nil
}

func validateSelector(selector dataframeSelector) error {
	for name, value := range map[string]string{"recipe": selector.Recipe, "output": selector.Output} {
		if strings.TrimSpace(value) == "" || strings.TrimSpace(value) != value {
			return fmt.Errorf("dataframe selector %s is required and must not have surrounding whitespace", name)
		}
	}
	return nil
}

func (j *job) recipeBindings(generation string) map[string]any {
	return map[string]any{
		"project":           j.projectID,
		"datasetGeneration": generation,
		"authResourcePaths": []string{fmt.Sprintf("/programs/%s/projects/%s", j.program, j.project)},
	}
}

// validateRecipe reads the immutable recipe registration from Loom.  This is
// intentionally done immediately before materialization: the server's
// registered recipe name, translation version, and output set are the source
// of truth, rather than a stale ETL binary default.
func (j *job) validateRecipe(generation string, selectors []dataframeSelector) (recipeValidation, error) {
	recipe := selectors[0].Recipe
	for _, selector := range selectors[1:] {
		if selector.Recipe != recipe {
			return recipeValidation{}, errors.New("dataframe selectors must use one recipe name")
		}
	}
	query := `mutation($input: ValidateDataframeRecipeInput!) { validateDataframeRecipe(input: $input) { name recipeDigest translationVersion outputs { name } } }`
	var response struct {
		Validation recipeValidation `json:"validateDataframeRecipe"`
	}
	if err := j.graphql(query, map[string]any{"input": map[string]any{
		"name":     recipe,
		"bindings": j.recipeBindings(generation),
	}}, &response); err != nil {
		return recipeValidation{}, fmt.Errorf("validate deployed Loom recipe: %w", err)
	}
	validation := response.Validation
	if validation.Name != recipe {
		return recipeValidation{}, fmt.Errorf("Loom validated recipe %q, want %q", validation.Name, recipe)
	}
	configuredVersion := strings.TrimSpace(selectors[0].TranslationVersion)
	if configuredVersion != "" && validation.TranslationVersion != configuredVersion {
		return recipeValidation{}, fmt.Errorf("Loom recipe %s translation version %q does not match configured %q", recipe, validation.TranslationVersion, configuredVersion)
	}
	wanted := make(map[string]struct{}, len(selectors))
	for _, selector := range selectors {
		if selector.Output == "" {
			return recipeValidation{}, errors.New("dataframe selector output is required")
		}
		if selector.TranslationVersion != "" && selector.TranslationVersion != validation.TranslationVersion {
			return recipeValidation{}, fmt.Errorf("Loom recipe %s translation version %q does not match configured %q", recipe, validation.TranslationVersion, selector.TranslationVersion)
		}
		wanted[selector.Output] = struct{}{}
	}
	actual := make(map[string]struct{}, len(validation.Outputs))
	for _, output := range validation.Outputs {
		if _, duplicate := actual[output.Name]; duplicate {
			return recipeValidation{}, fmt.Errorf("Loom recipe %s returned duplicate output %q", recipe, output.Name)
		}
		actual[output.Name] = struct{}{}
	}
	for output := range wanted {
		if _, ok := actual[output]; !ok {
			return recipeValidation{}, fmt.Errorf("Loom recipe %s is missing required output %q", recipe, output)
		}
	}
	return validation, nil
}

func sortedKeys(values map[string]struct{}) []string {
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}

func (j *job) materializeRecipe(generation string, validation recipeValidation) (recipeExecution, error) {
	input := map[string]any{"name": validation.Name, "bindings": j.recipeBindings(generation)}
	mutation := `mutation($input: MaterializeDataframeRecipeInput!) { materializeDataframeRecipeBundle(input: $input) { id name state sourceGeneration recipeDigest resolvedSchemaDigest error errorCode errorRetryable outputs { name state error errorCode } } }`
	var started struct {
		Execution recipeExecution `json:"materializeDataframeRecipeBundle"`
	}
	if err := j.graphql(mutation, map[string]any{"input": input}, &started); err != nil {
		return recipeExecution{}, fmt.Errorf("materialize Loom recipe bundle: %w", err)
	}
	if started.Execution.ID == "" {
		return recipeExecution{}, errors.New("Loom recipe materialization returned no execution ID")
	}
	slog.Info("materialized Loom recipe bundle", "execution_id", started.Execution.ID, "recipe", validation.Name, "translation_version", validation.TranslationVersion, "outputs", sortedValidationOutputs(validation), "generation", generation)
	if started.Execution.SourceGeneration != "" && started.Execution.SourceGeneration != generation {
		return recipeExecution{}, fmt.Errorf("execution %s source generation %q does not match %q", started.Execution.ID, started.Execution.SourceGeneration, generation)
	}
	if started.Execution.State == "FAILED" {
		return recipeExecution{}, recipeExecutionError(started.Execution)
	}
	if started.Execution.State == "READY" {
		if err := validateExecutionOutputs(started.Execution, validation); err != nil {
			return recipeExecution{}, err
		}
		return started.Execution, nil
	}

	query := `query($id: ID!) { dataframeRecipeExecution(id: $id) { id state sourceGeneration error errorCode errorRetryable phase outputs { name state error errorCode } } }`
	startedAt := time.Now()
	deadline := startedAt.Add(recipePollTimeout)
	lastPollLog := time.Now().Add(-operationProgressInterval)
	lastState := ""
	for time.Now().Before(deadline) {
		if time.Since(lastPollLog) >= operationProgressInterval {
			slog.Info("waiting for Loom recipe materialization", "execution_id", started.Execution.ID, "last_state", lastState, "elapsed", time.Since(startedAt))
			lastPollLog = time.Now()
		}
		var status struct {
			Execution *recipeExecution `json:"dataframeRecipeExecution"`
		}
		if err := j.graphql(query, map[string]any{"id": started.Execution.ID}, &status); err != nil {
			return recipeExecution{}, fmt.Errorf("poll Loom recipe materialization: %w", err)
		}
		if status.Execution == nil {
			return recipeExecution{}, errors.New("Loom recipe execution disappeared")
		}
		if status.Execution.State != lastState {
			slog.Info("Loom recipe materialization state changed", "execution_id", started.Execution.ID, "state", status.Execution.State, "phase", status.Execution.Phase)
			lastState = status.Execution.State
		}
		switch status.Execution.State {
		case "READY", "PUBLISHED": // PUBLISHED is retained for older Loom servers.
			if status.Execution.SourceGeneration != "" && status.Execution.SourceGeneration != generation {
				return recipeExecution{}, fmt.Errorf("execution %s source generation %q does not match %q", started.Execution.ID, status.Execution.SourceGeneration, generation)
			}
			if err := validateExecutionOutputs(*status.Execution, validation); err != nil {
				return recipeExecution{}, err
			}
			return *status.Execution, nil
		case "FAILED":
			return recipeExecution{}, recipeExecutionError(*status.Execution)
		}
		timer := time.NewTimer(recipePollInterval)
		select {
		case <-j.ctx.Done():
			timer.Stop()
			return recipeExecution{}, j.ctx.Err()
		case <-timer.C:
		}
	}
	return recipeExecution{}, fmt.Errorf("timed out waiting for Loom recipe execution %s", started.Execution.ID)
}

func (j *job) activateGeneration(generation, executionID string) error {
	query := url.Values{}
	query.Set("dataframe_execution_id", executionID)
	query.Set("auth_resource_path", fmt.Sprintf("/programs/%s/projects/%s", j.program, j.project))
	endpoint := fmt.Sprintf("%s/api/v1/datasets/%s/generations/%s/activate?%s", j.loomURL, url.PathEscape(j.projectID), url.PathEscape(generation), query.Encode())
	for attempt := 1; attempt <= maxRequestAttempts; attempt++ {
		ctx, cancel := context.WithTimeout(j.ctx, requestTimeout)
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, nil)
		if err != nil {
			cancel()
			return err
		}
		req.Header.Set("Authorization", "bearer "+j.token)
		resp, err := j.httpClient.Do(req)
		if err == nil && resp.StatusCode >= 200 && resp.StatusCode < 300 {
			resp.Body.Close()
			cancel()
			return nil
		}
		if err == nil {
			err = responseError(resp)
			resp.Body.Close()
		}
		cancel()
		if attempt == maxRequestAttempts || !retryableLoomError(err) {
			return err
		}
		if err := waitForRetry(j.ctx, attempt); err != nil {
			return err
		}
	}
	return errors.New("unreachable generation activation retry state")
}

func sortedValidationOutputs(validation recipeValidation) []string {
	outputs := make([]string, 0, len(validation.Outputs))
	for _, output := range validation.Outputs {
		outputs = append(outputs, output.Name)
	}
	sort.Strings(outputs)
	return outputs
}

func recipeExecutionError(execution recipeExecution) error {
	message := "Loom recipe execution failed"
	if execution.Error != nil && strings.TrimSpace(*execution.Error) != "" {
		message += ": " + *execution.Error
	}
	if execution.ErrorCode != "" {
		message += " (code=" + execution.ErrorCode + ")"
	}
	return errors.New(message)
}

// Older Loom resolver versions return an empty output list while the bundle
// itself is READY. When output metadata is present, enforce the complete
// configured set and each child's READY state; the server's atomic bundle
// publication remains the authority when the list is omitted.
func validateExecutionOutputs(execution recipeExecution, validation recipeValidation) error {
	if len(execution.Outputs) == 0 {
		return nil
	}
	wanted := make(map[string]struct{}, len(validation.Outputs))
	for _, output := range validation.Outputs {
		wanted[output.Name] = struct{}{}
	}
	seen := make(map[string]struct{}, len(execution.Outputs))
	for _, output := range execution.Outputs {
		if _, ok := wanted[output.Name]; !ok {
			return fmt.Errorf("Loom execution returned unexpected output %q", output.Name)
		}
		if _, duplicate := seen[output.Name]; duplicate {
			return fmt.Errorf("Loom execution returned duplicate output %q", output.Name)
		}
		seen[output.Name] = struct{}{}
		if output.State != "PUBLISHED" && output.State != "READY" {
			return fmt.Errorf("output %s completed with state %s", output.Name, output.State)
		}
	}
	for output := range wanted {
		if _, ok := seen[output]; !ok {
			return fmt.Errorf("Loom execution omitted required output %q", output)
		}
	}
	return nil
}

func retryableLoomError(err error) bool {
	if err == nil || errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
		return false
	}
	var httpErr httpError
	if errors.As(err, &httpErr) {
		return httpErr.Retryable
	}
	var graphErr loomGraphQLError
	if errors.As(err, &graphErr) {
		return graphErr.Retryable != nil && *graphErr.Retryable
	}
	return true
}

func waitForRetry(ctx context.Context, attempt int) error {
	delay := requestRetryDelay * time.Duration(attempt)
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

func (j *job) graphql(query string, variables map[string]any, out any) error {
	for attempt := 1; attempt <= maxRequestAttempts; attempt++ {
		err := j.graphqlAttempt(query, variables, out)
		if err == nil || attempt == maxRequestAttempts || !retryableLoomError(err) {
			return err
		}
		if err := waitForRetry(j.ctx, attempt); err != nil {
			return err
		}
	}
	return errors.New("unreachable GraphQL retry state")
}

func (j *job) graphqlAttempt(query string, variables map[string]any, out any) error {
	payload := map[string]any{"query": query, "variables": variables}
	body, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(j.ctx, requestTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, j.loomURL+"/graphql/graph", bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "bearer "+j.token)
	req.Header.Set("Content-Type", "application/json")
	resp, err := j.httpClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return responseError(resp)
	}
	var envelope struct {
		Data   json.RawMessage `json:"data"`
		Errors []struct {
			Message    string `json:"message"`
			Extensions struct {
				Code      string         `json:"code"`
				RequestID string         `json:"requestId"`
				Retryable *bool          `json:"retryable"`
				FieldPath []string       `json:"fieldPath"`
				Details   map[string]any `json:"details"`
			} `json:"extensions"`
		} `json:"errors"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&envelope); err != nil {
		return err
	}
	if len(envelope.Errors) > 0 {
		value := envelope.Errors[0]
		return loomGraphQLError{
			Message: value.Message, Code: value.Extensions.Code, RequestID: value.Extensions.RequestID,
			Retryable: value.Extensions.Retryable, FieldPath: value.Extensions.FieldPath, Details: value.Extensions.Details,
		}
	}
	return json.Unmarshal(envelope.Data, out)
}

func validateInput(in inputData) error {
	fields := map[string]string{
		"ghUserName": in.GHUserName, "ghToken": in.GHToken, "ghCommitHash": in.GHCommitHash,
		"ghRepoUrl": in.GHRepoURL, "profile": in.Profile,
		"APIEndpoint": in.APIEndpoint,
	}
	for name, value := range fields {
		if strings.TrimSpace(value) == "" {
			return fmt.Errorf("input data must contain a `%s`", name)
		}
	}
	return nil
}

func parseInput(raw string) (inputData, error) {
	var input inputData
	if err := json.Unmarshal([]byte(raw), &input); err != nil {
		return inputData{}, fmt.Errorf("parse INPUT_DATA: %w", err)
	}
	if strings.TrimSpace(input.Method) == "" {
		return inputData{}, errors.New("input data must contain a `method`")
	}
	return input, nil
}

func redactInputPacket(raw string) string {
	var packet map[string]any
	if err := json.Unmarshal([]byte(raw), &packet); err != nil {
		return fmt.Sprintf("<invalid JSON: %s>", err)
	}
	for key := range packet {
		name := strings.ToLower(key)
		if strings.Contains(name, "token") || strings.Contains(name, "secret") || strings.Contains(name, "password") {
			packet[key] = "[REDACTED]"
		}
	}
	encoded, err := json.Marshal(packet)
	if err != nil {
		return "<unable to encode INPUT_DATA>"
	}
	return string(encoded)
}

func fetchUser(ctx context.Context, hostname, token string) (map[string]any, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, hostname+"/user/user", nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "bearer "+token)
	resp, err := (&http.Client{Timeout: requestTimeout}).Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, responseError(resp)
	}
	var user map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&user); err != nil {
		return nil, err
	}
	return user, nil
}

func cloneRepository(ctx context.Context, in inputData, workspace string) error {
	u, err := url.Parse("https://" + strings.TrimPrefix(strings.TrimPrefix(in.GHRepoURL, "https://"), "http://"))
	if err != nil {
		return fmt.Errorf("parse repository URL: %w", err)
	}
	u.User = url.UserPassword(in.GHUserName, in.GHToken)
	if err := runCommand(ctx, workspace, "git", "clone", u.String()); err != nil {
		return fmt.Errorf("clone repository: %w", err)
	}
	return nil
}

func checkoutRepository(ctx context.Context, dir, commit string) error {
	if err := runGit(ctx, dir, "-c", "advice.detachedHead=false", "checkout", commit); err != nil {
		return fmt.Errorf("checkout %s: %w", commit, err)
	}
	return nil
}

func runGit(ctx context.Context, dir string, args ...string) error {
	return runCommand(ctx, dir, "git", args...)
}

func runOperation(name string, fn func() error, attrs ...any) error {
	start := time.Now()
	startAttrs := append([]any{"operation", name}, attrs...)
	slog.Info("ETL operation started", startAttrs...)

	err := fn()
	endAttrs := append([]any{"operation", name, "elapsed", time.Since(start)}, attrs...)
	if err != nil {
		endAttrs = append(endAttrs, "error", err)
		slog.Error("ETL operation failed", endAttrs...)
		return err
	}
	slog.Info("ETL operation completed", endAttrs...)
	return nil
}

func runCommand(ctx context.Context, dir, name string, args ...string) error {
	cmd := exec.CommandContext(ctx, name, args...)
	cmd.Dir = dir
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stdout
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("%s failed: %w", name, err)
	}
	return nil
}

func withWorkingDirectory(dir string, fn func() error) error {
	workingDirectoryMu.Lock()
	defer workingDirectoryMu.Unlock()
	old, err := os.Getwd()
	if err != nil {
		return err
	}
	if err := os.Chdir(dir); err != nil {
		return err
	}
	defer os.Chdir(old)
	return fn()
}

func moveGeneratedMetadata(targetDir, loadDir string) error {
	if err := os.MkdirAll(loadDir, 0o755); err != nil {
		return fmt.Errorf("create metadata load directory: %w", err)
	}
	files, err := listNDJSON(filepath.Join(targetDir, metaDir))
	if err != nil {
		return err
	}
	for _, source := range files {
		destination := filepath.Join(loadDir, filepath.Base(source))
		if err := os.Rename(source, destination); err != nil {
			return fmt.Errorf("move %s: %w", filepath.Base(source), err)
		}
	}
	return nil
}

func validateDocumentReferences(dir string) error {
	path := filepath.Join(dir, "DocumentReference.ndjson")
	input, err := os.Open(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	defer input.Close()

	seenIDs := make(map[string]struct{})
	scanner := bufio.NewScanner(input)
	scanner.Buffer(make([]byte, 64*1024), 10*1024*1024)
	for scanner.Scan() {
		var resource struct {
			ID string `json:"id"`
		}
		if err := json.Unmarshal(scanner.Bytes(), &resource); err != nil {
			return fmt.Errorf("parse DocumentReference: %w", err)
		}
		resource.ID = strings.TrimSpace(resource.ID)
		if resource.ID == "" {
			return errors.New("DocumentReference is missing id")
		}
		if _, exists := seenIDs[resource.ID]; exists {
			return fmt.Errorf("duplicate DocumentReference id %q", resource.ID)
		}
		seenIDs[resource.ID] = struct{}{}
	}
	if err := scanner.Err(); err != nil {
		return err
	}
	return nil
}

func listNDJSON(dir string) ([]string, error) {
	return listMatching(dir, ".ndjson")
}

func listJSON(dir string) ([]string, error) {
	return listMatching(dir, ".json")
}

func listMatching(dir, suffix string) ([]string, error) {
	entries, err := os.ReadDir(dir)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	paths := make([]string, 0, len(entries))
	for _, entry := range entries {
		if !entry.IsDir() && strings.HasSuffix(entry.Name(), suffix) {
			paths = append(paths, filepath.Join(dir, entry.Name()))
		}
	}
	return paths, nil
}

func hydratePointers(ctx context.Context, endpoint, token, organization, project, root string, paths []string) error {
	remote, err := gitdrs.New(gitdrs.Options{
		Endpoint:     endpoint,
		AccessToken:  token,
		Organization: organization,
		Project:      project,
		Logger:       slog.Default(),
	})
	if err != nil {
		return fmt.Errorf("create Git-DRS client: %w", err)
	}
	files := make([]gitdrs.File, 0, len(paths))
	for _, path := range paths {
		file, ok, err := readLFSPointer(root, path)
		if err != nil {
			return err
		}
		if ok {
			files = append(files, file)
		}
	}
	if len(files) == 0 {
		return nil
	}
	slog.Info("starting Git-DRS pointer hydration", "files", len(files), "organization", organization, "project", project)
	// The git-drs downloader treats an existing destination as a partial
	// download and resumes from its current size. LFS pointer files already
	// occupy those destinations, so leaving them in place produces a corrupt
	// payload consisting of the pointer prefix plus the payload tail.
	for _, file := range files {
		path := filepath.Join(root, filepath.FromSlash(file.Path))
		if err := os.Remove(path); err != nil && !os.IsNotExist(err) {
			return fmt.Errorf("remove Git-DRS pointer %s before hydration: %w", file.Path, err)
		}
	}
	if err := remote.Pull(ctx, gitdrs.PullOptions{Root: root, Files: files}); err != nil {
		return err
	}
	slog.Info("completed Git-DRS pointer hydration", "files", len(files))
	return nil
}

func readLFSPointer(root, path string) (gitdrs.File, bool, error) {
	relative, err := filepath.Rel(root, path)
	if err != nil {
		return gitdrs.File{}, false, fmt.Errorf("resolve pointer path %s: %w", path, err)
	}
	input, err := os.Open(path)
	if err != nil {
		return gitdrs.File{}, false, err
	}
	defer input.Close()

	var oid string
	var size int64 = -1
	scanner := bufio.NewScanner(input)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		switch {
		case strings.HasPrefix(line, "oid "):
			oid = strings.TrimSpace(strings.TrimPrefix(line, "oid "))
		case strings.HasPrefix(line, "size "):
			size, err = strconv.ParseInt(strings.TrimSpace(strings.TrimPrefix(line, "size ")), 10, 64)
			if err != nil {
				return gitdrs.File{}, false, fmt.Errorf("parse LFS pointer size in %s: %w", path, err)
			}
		}
	}
	if err := scanner.Err(); err != nil {
		return gitdrs.File{}, false, err
	}
	if oid == "" || size < 0 {
		return gitdrs.File{}, false, nil
	}
	return gitdrs.File{Path: filepath.ToSlash(relative), OID: oid, Size: size}, true, nil
}

func responseError(resp *http.Response) error {
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
	var envelope struct {
		Error httpError `json:"error"`
	}
	if err := json.Unmarshal(body, &envelope); err == nil && envelope.Error.Message != "" {
		return envelope.Error
	}
	return fmt.Errorf("HTTP %s: %s", resp.Status, strings.TrimSpace(string(body)))
}

func splitProjectID(projectID string) (string, string, error) {
	parts := strings.Split(projectID, "-")
	if len(parts) != 2 || parts[0] == "" || parts[1] == "" {
		return "", "", errors.New("project_id must be in the format <program>-<project>")
	}
	return parts[0], parts[1], nil
}

func repoName(raw string) string {
	base := filepath.Base(strings.TrimSuffix(raw, "/"))
	return strings.TrimSuffix(base, ".git")
}

func requiredEnv(name string) (string, error) {
	value := strings.TrimSpace(os.Getenv(name))
	if value == "" {
		return "", fmt.Errorf("%s not found in environment", name)
	}
	return value, nil
}

func envOr(name, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(name)); value != "" {
		return value
	}
	return fallback
}

func stringValue(value any) string {
	if value, ok := value.(string); ok {
		return value
	}
	return ""
}

func fatalOutput(output *outputData, err error) {
	slog.Error("ETL failed", "error", err)
	output.Logs = append(output.Logs, err.Error())
	writeOutput(output)
	os.Exit(1)
}

func writeOutput(output *outputData) {
	encoded, err := json.Marshal(output)
	if err != nil {
		fmt.Fprintf(os.Stdout, "[out] {\"logs\":[%q]}\n", err.Error())
		return
	}
	fmt.Fprintf(os.Stdout, "[out] %s\n", encoded)
}
