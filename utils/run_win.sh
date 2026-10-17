cp /root/sandbox-log/config_win.yaml /root/sandbox-log/openclaw.json /root/sandbox-log/openclaw-task.tar '$run_dir/'; 
docker cp '$run_dir' '$toolbox:/tmp/'; 
docker exec '$toolbox' sh -lc 'PYTHONPATH=/workspace/src 
python3 -u $run_dir/run.py --config $run_dir/config_win.yaml 
--openclaw-config $run_dir/openclaw.json 
--task-tar $run_dir/openclaw-task.tar --output $run_dir/evidence'" || run_status=$?


python win_demo.py --config /root/sandbox-log/config_win.yaml --openclaw-config /root/sandbox-log/openclaw.json --task-tar /
root/sandbox-log/openclaw-task.tar --output /root/zengxiang/openclaw-hive/win_demo_1