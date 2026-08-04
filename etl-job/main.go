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
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/calypr/forge/metadata"
	gitdrs "github.com/calypr/git-drs/client"
)

const (
	metaDir              = "META"
	configDir            = "CONFIG"
	requestTimeout       = 5 * time.Minute
	uploadTimeout        = time.Hour
	recipePollInterval   = 2 * time.Second
	recipePollTimeout    = 30 * time.Minute
	defaultRecipeName    = "calypr-meta-default"
	defaultRepositoryDir = "/root/repo"
)

var workingDirectoryMu sync.Mutex

type inputData struct {
	Method       string `json:"method"`
	ProjectID    string `json:"projectId"`
	GHUserName   string `json:"ghUserName"`
	GHToken      string `json:"ghToken"`
	GHCommitHash string `json:"ghCommitHash"`
	GHRepoURL    string `json:"ghRepoUrl"`
	BucketName   string `json:"bucketName"`
	Profile      string `json:"profile"`
	APIEndpoint  string `json:"APIEndpoint"`
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
	input      inputData
	output     *outputData
	httpClient *http.Client
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

	input, err := parseInput()
	if err != nil {
		fatalOutput(output, err)
		return
	}
	program, project, err := splitProjectID(input.ProjectID)
	if err != nil {
		fatalOutput(output, err)
		return
	}

	user, err := fetchUser(ctx, hostname, token)
	if err != nil {
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
		input:      input,
		output:     output,
		httpClient: &http.Client{Timeout: requestTimeout},
	}

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
	if err := validateInput(j.input); err != nil {
		return err
	}

	loadPath := filepath.Join(defaultRepositoryDir, j.project)
	workspaceDir := filepath.Dir(loadPath)
	if err := os.MkdirAll(workspaceDir, 0o755); err != nil {
		return fmt.Errorf("create repository workspace: %w", err)
	}
	targetDir := filepath.Join(workspaceDir, repoName(j.input.GHRepoURL))
	defer func() {
		_ = os.RemoveAll(loadPath)
		_ = os.RemoveAll(targetDir)
	}()

	if err := cloneRepository(j.ctx, j.input, workspaceDir); err != nil {
		return err
	}
	if err := checkoutRepository(j.ctx, targetDir, j.input.GHCommitHash); err != nil {
		return err
	}
	if j.input.Profile != "origin" {
		if err := runGit(j.ctx, targetDir, "remote", "rename", "origin", j.input.Profile); err != nil {
			return fmt.Errorf("rename git remote: %w", err)
		}
	}

	if err := withWorkingDirectory(targetDir, func() error {
		logger := slog.New(slog.NewTextHandler(os.Stdout, nil))
		if err := gitdrs.InitializeRepository(logger); err != nil {
			return fmt.Errorf("initialize git-drs: %w", err)
		}
		if err := gitdrs.ConfigureGen3Remote(gitdrs.Gen3RemoteOptions{
			RemoteName: j.input.Profile,
			Token:      j.token,
			Bucket:     j.input.BucketName,
			Scope:      j.program + "/" + j.project,
			Logger:     logger,
		}); err != nil {
			return fmt.Errorf("configure git-drs remote: %w", err)
		}
		return nil
	}); err != nil {
		return err
	}

	metaFiles, err := listNDJSON(filepath.Join(targetDir, metaDir))
	if err != nil {
		return fmt.Errorf("discover META files: %w", err)
	}
	configFiles, err := listJSON(filepath.Join(targetDir, configDir))
	if err != nil {
		return fmt.Errorf("discover CONFIG files: %w", err)
	}
	if len(metaFiles) > 0 || len(configFiles) > 0 {
		drsEndpoint := strings.TrimRight(strings.TrimSpace(j.input.APIEndpoint), "/")
		if drsEndpoint == "" {
			return fmt.Errorf("Sower input APIEndpoint is required for Git-DRS hydration")
		}
		if err := hydratePointers(j.ctx, drsEndpoint, j.token, j.program, j.project, targetDir, append(metaFiles, configFiles...)); err != nil {
			return fmt.Errorf("pull repository files with git-drs: %w", err)
		}
	}

	var reconciliation metadata.ReconcileReport
	if err := withWorkingDirectory(targetDir, func() error {
		var err error
		reconciliation, err = metadata.ReconcileGitPointers(j.ctx, metadata.ReconcileOptions{
			RepositoryRoot: targetDir,
			GitRef:         j.input.GHCommitHash,
			FHIRDirectory:  filepath.Join(targetDir, metaDir),
			ProfileName:    j.input.Profile,
			GitRemoteName:  j.input.Profile,
		})
		return err
	}); err != nil {
		return fmt.Errorf("generate forge metadata: %w", err)
	}
	message := fmt.Sprintf("Git/Syfon reconciliation: pointers=%d authored=%d matched=%d generated=%d metadata_only=%d authored_without_sha=%d", reconciliation.GitPointers, reconciliation.AuthoredRows, reconciliation.MatchedRows, reconciliation.GeneratedRows, len(reconciliation.MetadataOnlySHA256), reconciliation.AuthoredRowsWithoutSHA)
	slog.Info(message)
	if len(reconciliation.MetadataOnlySHA256) > 0 {
		warning := fmt.Sprintf("WARNING: retained %d authored DocumentReference SHA256 values not present in the Git snapshot", len(reconciliation.MetadataOnlySHA256))
		slog.Warn(warning)
	}
	if err := moveGeneratedMetadata(targetDir, loadPath); err != nil {
		return err
	}
	if err := validateDocumentReferences(loadPath); err != nil {
		return err
	}
	if err := j.uploadConfigs(filepath.Join(targetDir, configDir)); err != nil {
		return err
	}
	if err := j.uploadMetadata(loadPath); err != nil {
		return err
	}
	if err := j.materialize(); err != nil {
		return err
	}
	return nil
}

func (j *job) uploadMetadata(dir string) error {
	files, err := listNDJSON(dir)
	if err != nil {
		return fmt.Errorf("discover generated metadata: %w", err)
	}
	if len(files) == 0 {
		return fmt.Errorf("no NDJSON files found in %s", dir)
	}
	for _, path := range files {
		resourceType := strings.TrimSuffix(filepath.Base(path), filepath.Ext(path))
		if err := j.putResource(resourceType, path); err != nil {
			return fmt.Errorf("load %s into Loom: %w", resourceType, err)
		}
		j.output.Files = append(j.output.Files, path)
	}
	return nil
}

func (j *job) putResource(resourceType, path string) error {
	file, err := os.Open(path)
	if err != nil {
		return err
	}
	defer file.Close()

	var body bytes.Buffer
	mw := multipart.NewWriter(&body)
	part, err := mw.CreateFormFile("file", filepath.Base(path))
	if err != nil {
		return err
	}
	if _, err := io.Copy(part, file); err != nil {
		return err
	}
	if err := mw.WriteField("auth_resource_path", fmt.Sprintf("/programs/%s/projects/%s", j.program, j.project)); err != nil {
		return err
	}
	if err := mw.Close(); err != nil {
		return err
	}

	endpoint := fmt.Sprintf("%s/api/v1/projects/%s/resources/%s", j.loomURL, url.PathEscape(j.projectID), url.PathEscape(resourceType))
	ctx, cancel := context.WithTimeout(j.ctx, uploadTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodPut, endpoint, &body)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "bearer "+j.token)
	req.Header.Set("Content-Type", mw.FormDataContentType())
	resp, err := j.httpClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return responseError(resp)
	}
	slog.Info("loaded resource into Loom", "file", filepath.Base(path), "resource_type", resourceType)
	return nil
}

func (j *job) uploadConfigs(dir string) error {
	files, err := listJSON(dir)
	if err != nil {
		return err
	}
	for _, path := range files {
		base := strings.TrimSuffix(filepath.Base(path), filepath.Ext(path))
		parts := strings.Split(base, "-")
		if len(parts) != 2 || parts[0] == "" || parts[1] == "" {
			slog.Warn("skipping config file without project suffix", "file", filepath.Base(path))
			continue
		}
		content, err := os.ReadFile(path)
		if err != nil {
			return fmt.Errorf("read config %s: %w", filepath.Base(path), err)
		}
		endpoint := fmt.Sprintf("%s/gecko/explorer/%s", j.hostname, url.PathEscape(base))
		req, err := http.NewRequestWithContext(j.ctx, http.MethodPut, endpoint, bytes.NewReader(content))
		if err != nil {
			return err
		}
		req.Header.Set("Authorization", "bearer "+j.token)
		req.Header.Set("Content-Type", "application/json")
		resp, err := j.httpClient.Do(req)
		if err != nil {
			return fmt.Errorf("upload config %s: %w", filepath.Base(path), err)
		}
		if resp.StatusCode < 200 || resp.StatusCode >= 300 {
			err := responseError(resp)
			resp.Body.Close()
			return fmt.Errorf("upload config %s: %w", filepath.Base(path), err)
		}
		resp.Body.Close()
		slog.Info("uploaded Gecko explorer config", "status", resp.StatusCode, "config", base)
	}
	return nil
}

func (j *job) materialize() error {
	name := envOr("LOOM_RECIPE_NAME", defaultRecipeName)
	input := map[string]any{
		"name": name,
		"bindings": map[string]any{
			"project":           j.projectID,
			"authResourcePaths": []string{fmt.Sprintf("/programs/%s/projects/%s", j.program, j.project)},
		},
	}
	mutation := `mutation($input: MaterializeDataframeRecipeInput!) { materializeDataframeRecipeBundle(input: $input) { id state name error } }`
	var started struct {
		Materialize struct {
			ID    string  `json:"id"`
			State string  `json:"state"`
			Name  string  `json:"name"`
			Error *string `json:"error"`
		} `json:"materializeDataframeRecipeBundle"`
	}
	if err := j.graphql(mutation, map[string]any{"input": input}, &started); err != nil {
		return fmt.Errorf("start Loom recipe materialization: %w", err)
	}
	if started.Materialize.ID == "" {
		return errors.New("Loom recipe materialization returned no execution ID")
	}
	slog.Info("started Loom recipe materialization", "recipe", name, "execution_id", started.Materialize.ID)

	query := `query($id: ID!) { dataframeRecipeExecution(id: $id) { id state error } }`
	deadline := time.Now().Add(recipePollTimeout)
	for time.Now().Before(deadline) {
		var status struct {
			Execution *struct {
				ID    string  `json:"id"`
				State string  `json:"state"`
				Error *string `json:"error"`
			} `json:"dataframeRecipeExecution"`
		}
		if err := j.graphql(query, map[string]any{"id": started.Materialize.ID}, &status); err != nil {
			return fmt.Errorf("poll Loom recipe materialization: %w", err)
		}
		if status.Execution == nil {
			return errors.New("Loom recipe execution disappeared")
		}
		switch status.Execution.State {
		case "READY":
			slog.Info("Loom recipe materialization is ready", "execution_id", started.Materialize.ID)
			return nil
		case "FAILED":
			if status.Execution.Error != nil {
				return fmt.Errorf("Loom recipe execution failed: %s", *status.Execution.Error)
			}
			return errors.New("Loom recipe execution failed")
		}
		time.Sleep(recipePollInterval)
	}
	return fmt.Errorf("timed out waiting for Loom recipe execution %s", started.Materialize.ID)
}

func (j *job) graphql(query string, variables map[string]any, out any) error {
	payload := map[string]any{"query": query, "variables": variables}
	body, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(j.ctx, http.MethodPost, j.loomURL+"/graphql/graph", bytes.NewReader(body))
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
			Message string `json:"message"`
		} `json:"errors"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&envelope); err != nil {
		return err
	}
	if len(envelope.Errors) > 0 {
		return errors.New(envelope.Errors[0].Message)
	}
	return json.Unmarshal(envelope.Data, out)
}

func validateInput(in inputData) error {
	fields := map[string]string{
		"ghUserName": in.GHUserName, "ghToken": in.GHToken, "ghCommitHash": in.GHCommitHash,
		"ghRepoUrl": in.GHRepoURL, "bucketName": in.BucketName, "profile": in.Profile,
		"APIEndpoint": in.APIEndpoint,
	}
	for name, value := range fields {
		if strings.TrimSpace(value) == "" {
			return fmt.Errorf("input data must contain a `%s`", name)
		}
	}
	return nil
}

func parseInput() (inputData, error) {
	raw, err := requiredEnv("INPUT_DATA")
	if err != nil {
		return inputData{}, err
	}
	var input inputData
	if err := json.Unmarshal([]byte(raw), &input); err != nil {
		return inputData{}, fmt.Errorf("parse INPUT_DATA: %w", err)
	}
	if strings.TrimSpace(input.Method) == "" {
		return inputData{}, errors.New("input data must contain a `method`")
	}
	return input, nil
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
	if err := runGit(ctx, dir, "checkout", commit); err != nil {
		return fmt.Errorf("checkout %s: %w", commit, err)
	}
	return nil
}

func runGit(ctx context.Context, dir string, args ...string) error {
	return runCommand(ctx, dir, "git", args...)
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
