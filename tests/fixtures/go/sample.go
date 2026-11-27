// 分段器 fixtures（Go）—— 覆盖 Go 特有的三个坑
package fixture

// 坑 1：前导区必须含 `package` 子句，光补 import 不补 package 块根本解析不了。
import (
	"encoding/json"

	fmt "fmt"
)

// 坑 2：顶层 const/var 块要算可切单元，且不能被当成前导区整块吞掉
const (
	MaxItems = 100
	MinItems = 1
)

var (
	ErrTooMany = fmt.Errorf("too many")
	Defaults   = map[string]int{"retries": 3}
)

// 坑 3：分组 type 声明里每个 spec 都要能认出名字
type (
	Item struct {
		ID   string
		Name string
	}

	Store struct {
		items []Item
	}
)

func Load(raw []byte) ([]Item, error) {
	var out []Item
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, err
	}
	return out, nil
}

func (s *Store) Add(it Item) error {
	if len(s.items) >= MaxItems {
		return ErrTooMany
	}
	s.items = append(s.items, it)
	return nil
}

// 坑 4：receiver 方法要带 receiver 类型标签，日志里看得出归属
func (s *Store) Size() int {
	return len(s.items)
}
