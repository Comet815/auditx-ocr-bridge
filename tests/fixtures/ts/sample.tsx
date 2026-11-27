// 坑 5：JSX 只在 tsx 语法下合法；用 typescript 语法解析会一片 ERROR
interface Props {
  title: string;
  count?: number;
}

function Card({ title, count = 0 }: Props) {
  const label = `${title} × ${count}`;
  return (
    <div className="card">
      <span>{label}</span>
    </div>
  );
}

const List = (props: Props) => <ul><li>{props.title}</li></ul>;

export { Card, List };
export type { Props };
