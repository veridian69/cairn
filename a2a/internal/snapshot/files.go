package snapshot

import (
	"crypto/sha256"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"syscall"

	"github.com/veridian69/cairn/a2a/internal/maintenance"
	"golang.org/x/sys/unix"
)

const snapshotSpaceOverhead = uint64(64 * 1024)

func copyTree(source, destination string) error {
	if err := os.Mkdir(destination, 0700); err != nil {
		return err
	}
	return filepath.WalkDir(source, func(path string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if path == source {
			return nil
		}
		relative, err := filepath.Rel(source, path)
		if err != nil {
			return err
		}
		if filepath.ToSlash(relative) == "memory.db.lock" || filepath.ToSlash(relative) == maintenance.StoreLockName {
			return nil
		}
		target := filepath.Join(destination, relative)
		info, err := entry.Info()
		if err != nil {
			return err
		}
		switch {
		case entry.Type()&os.ModeSymlink != 0:
			return fmt.Errorf("refusing symlink %s", path)
		case entry.IsDir():
			return os.Mkdir(target, 0700)
		case info.Mode().IsRegular():
			if stat, ok := info.Sys().(*syscall.Stat_t); ok && stat.Nlink != 1 {
				return fmt.Errorf("refusing hard-linked runtime file %s", path)
			}
			return copyFile(path, target)
		default:
			return fmt.Errorf("refusing non-regular runtime entry %s", path)
		}
	})
}

func copyFile(source, destination string) error {
	input, err := os.Open(source)
	if err != nil {
		return err
	}
	defer input.Close()
	output, err := os.OpenFile(destination, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		return err
	}
	_, copyErr := io.Copy(output, input)
	syncErr := output.Sync()
	closeErr := output.Close()
	if copyErr != nil {
		return copyErr
	}
	if syncErr != nil {
		return syncErr
	}
	return closeErr
}

func writeFileSynced(path string, data []byte, mode os.FileMode) error {
	file, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, mode)
	if err != nil {
		return err
	}
	if _, err := file.Write(data); err != nil {
		_ = file.Close()
		return err
	}
	if err := file.Sync(); err != nil {
		_ = file.Close()
		return err
	}
	return file.Close()
}

func syncTree(root string) error {
	var directories []string
	err := filepath.WalkDir(root, func(path string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if entry.Type()&os.ModeSymlink != 0 {
			return fmt.Errorf("refusing symlink %s", path)
		}
		if entry.IsDir() {
			directories = append(directories, path)
			return nil
		}
		info, err := entry.Info()
		if err != nil {
			return err
		}
		if !info.Mode().IsRegular() {
			return fmt.Errorf("refusing non-regular snapshot entry %s", path)
		}
		file, err := os.Open(path)
		if err != nil {
			return err
		}
		syncErr := file.Sync()
		closeErr := file.Close()
		if syncErr != nil {
			return syncErr
		}
		return closeErr
	})
	if err != nil {
		return err
	}
	for index := len(directories) - 1; index >= 0; index-- {
		if err := syncDirectory(directories[index]); err != nil {
			return err
		}
	}
	return nil
}

func treeBytes(root string) (uint64, uint64, error) {
	var total uint64
	var objects uint64
	err := filepath.WalkDir(root, func(path string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			if path == root && os.IsNotExist(walkErr) {
				return nil
			}
			return walkErr
		}
		if path == root {
			return nil
		}
		objects++
		if entry.IsDir() {
			return nil
		}
		relative, err := filepath.Rel(root, path)
		if err != nil {
			return err
		}
		if filepath.ToSlash(relative) == "memory.db.lock" || filepath.ToSlash(relative) == maintenance.StoreLockName {
			return nil
		}
		info, err := entry.Info()
		if err != nil {
			return err
		}
		switch {
		case entry.Type()&os.ModeSymlink != 0:
			return fmt.Errorf("refusing symlink %s", path)
		case !info.Mode().IsRegular():
			return fmt.Errorf("refusing non-regular runtime entry %s", path)
		}
		if stat, ok := info.Sys().(*syscall.Stat_t); ok && stat.Nlink != 1 {
			return fmt.Errorf("refusing hard-linked runtime file %s", path)
		}
		if info.Size() < 0 || uint64(info.Size()) > ^uint64(0)-total {
			return fmt.Errorf("runtime size overflow at %s", path)
		}
		total += uint64(info.Size())
		return nil
	})
	return total, objects, err
}

func snapshotBytesRequired(dataBytes, dataObjects uint64, configBytes int64) uint64 {
	required := saturatedAdd(dataBytes, snapshotSpaceOverhead)
	if configBytes > 0 {
		required = saturatedAdd(required, uint64(configBytes))
	}
	const stagingOverheadPerFile = uint64(4096)
	if dataObjects > ^uint64(0)/stagingOverheadPerFile {
		return ^uint64(0)
	}
	return saturatedAdd(required, dataObjects*stagingOverheadPerFile)
}

func saturatedAdd(left, right uint64) uint64 {
	if right > ^uint64(0)-left {
		return ^uint64(0)
	}
	return left + right
}

func filesystemAvailableBytes(path string) (uint64, error) {
	var stats unix.Statfs_t
	if err := unix.Statfs(path, &stats); err != nil {
		return 0, err
	}
	blockSize := uint64(stats.Bsize)
	availableBlocks := uint64(stats.Bavail)
	if blockSize != 0 && availableBlocks > ^uint64(0)/blockSize {
		return ^uint64(0), nil
	}
	return availableBlocks * blockSize, nil
}

func (m *Manager) requireSpace(path string, required uint64) error {
	available, err := m.availableBytes(path)
	if err != nil {
		return fmt.Errorf("checking free space at %s: %w", path, err)
	}
	if available < required {
		return fmt.Errorf(
			"insufficient free space at %s: need %d bytes, have %d",
			path, required, available,
		)
	}
	return nil
}

func sameFilesystem(left, right string) (bool, error) {
	var leftStat unix.Stat_t
	if err := unix.Stat(left, &leftStat); err != nil {
		return false, err
	}
	var rightStat unix.Stat_t
	if err := unix.Stat(right, &rightStat); err != nil {
		return false, err
	}
	return leftStat.Dev == rightStat.Dev, nil
}

func inventory(root string) ([]fileInfo, int64, error) {
	var files []fileInfo
	var total int64
	err := filepath.WalkDir(root, func(path string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if entry.IsDir() {
			return nil
		}
		info, err := entry.Info()
		if err != nil {
			return err
		}
		if !info.Mode().IsRegular() {
			return fmt.Errorf("refusing non-regular snapshot entry %s", path)
		}
		if stat, ok := info.Sys().(*syscall.Stat_t); ok && stat.Nlink != 1 {
			return fmt.Errorf("refusing hard-linked snapshot file %s", path)
		}
		relative, err := filepath.Rel(root, path)
		if err != nil {
			return err
		}
		if relative == "manifest.json" {
			return nil
		}
		digest, err := hashFile(path)
		if err != nil {
			return err
		}
		files = append(files, fileInfo{
			Path: filepath.ToSlash(relative), Size: info.Size(),
			SHA256: digest, Mode: uint32(info.Mode().Perm()),
		})
		total += info.Size()
		return nil
	})
	sort.Slice(files, func(i, j int) bool { return files[i].Path < files[j].Path })
	return files, total, err
}

func hashFile(path string) (string, error) {
	file, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer file.Close()
	digest := sha256.New()
	if _, err := io.Copy(digest, file); err != nil {
		return "", err
	}
	return fmt.Sprintf("%x", digest.Sum(nil)), nil
}

func pathWithin(parent, child string) bool {
	relative, err := filepath.Rel(parent, child)
	if err != nil {
		return false
	}
	return relative == "." || (relative != ".." && !strings.HasPrefix(relative, ".."+string(os.PathSeparator)))
}
